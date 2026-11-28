#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Analysis-only crypto futures signal scanner (NO order execution).
Real-time Price Action + Liquidity Hunt (Sweep + MSS) + Active OB/FVG + Fibonacci (0.618-0.786).
Produces chart-aligned Entry, Stop Loss, and structural/Fibonacci Targets (TP1, TP2, TP3).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("signal_scanner")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
LBANK_BASE = os.getenv("LBANK_BASE_URL", "https://api.lbkex.com").rstrip("/")
DEFAULT_SYMBOLS = (
    "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT,"
    "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,"
    "DOT_USDT,LTC_USDT,ATOM_USDT,NEAR_USDT,BCH_USDT,"
    "TRX_USDT,HBAR_USDT,ICP_USDT,FIL_USDT,ETC_USDT,XLM_USDT,ALGO_USDT,AGT_USDT,AKE_USDT"
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 75.0)
RISK_PCT = _env_float("RISK_PCT", 1.0)
TARGET_LEVERAGE = _env_float("TARGET_LEVERAGE", _env_float("DEFAULT_LEVERAGE", 5.0))
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 10.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)

# Real-time chart alignment parameters
MAX_ENTRY_DIST_PCT = _env_float("MAX_ENTRY_DIST_PCT", 1.5)   # max distance from live price to entry
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 2.2)               # max SL distance %
MIN_STOP_PCT = _env_float("MIN_STOP_PCT", 0.25)              # min SL distance %
MAX_STOP_ATR = 2.8
MIN_STOP_ATR = 0.45
MIN_ATR_PCT = 0.0004
MAX_ATR_PCT = 0.03
MIN_VOL_RATIO = 0.5
MIN_CANDLES = 100

FEE_PCT = _env_float("FEE_PCT", 0.06)
MIN_RR1_NET = _env_float("MIN_RR1_NET", 1.1)
MAX_TP_RR = _env_float("MAX_TP_RR", 10.0)
MSS_MAX_DELAY = 8
MSS_MIN_BODY = 0.75
ACTIVE_SWING_BARS = int(_env_float("ACTIVE_SWING_BARS", 48))  # focus on recent active chart leg

CAPITAL_USDT = _env_float("ACCOUNT_EQUITY_USDT", 1000.0)
PLAN_FILE = os.getenv("PLAN_FILE", "plans_state.json")
PLAN_TTL_H = _env_float("PLAN_TTL_H", 0.0)  # 0 = always live chart calculation

TF_MAP: Dict[str, Tuple[str, int]] = {
    "1m": ("minute1", 60),
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

TREND_FA = {"UP": "صعودی", "DOWN": "نزولی", "RANGE": "رنج"}
TREND_ARROW = {"UP": "↑", "DOWN": "↓", "RANGE": "↔"}


class ScanError(Exception):
    """Recoverable error for a single symbol / request."""


# --------------------------------------------------------------------------
# HTTP with throttling and retries
# --------------------------------------------------------------------------
_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def http_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    retries: int = 4,
    timeout: float = 15.0,
) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"User-Agent": "signal-scanner/2.0", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code == 429 or e.code >= 500:
                delay = min(float(2 ** attempt), 25.0)
                time.sleep(delay)
                continue
            raise ScanError(last_err)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network: %s" % (getattr(e, "reason", None) or e)
            time.sleep(min(2 ** attempt, 15))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON"
            time.sleep(min(2 ** attempt, 15))
    raise ScanError("request failed (%s)" % last_err)


# --------------------------------------------------------------------------
# Market Data
# --------------------------------------------------------------------------
class Candles:
    def __init__(self, rows: List[Tuple[float, float, float, float, float, float]]):
        self.t = [r[0] for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] for r in rows]

    def __len__(self) -> int:
        return len(self.c)


def fetch_klines(symbol: str, tf: str, size: int = 250) -> Candles:
    typ, sec = TF_MAP[tf]
    now = int(time.time())
    start = now - size * sec
    data = http_json(
        LBANK_BASE + "/v2/kline.do",
        {"symbol": symbol.lower(), "size": size, "type": typ, "time": start},
    )
    if not isinstance(data, dict) or str(data.get("result", "")).lower() != "true":
        raise ScanError("kline %s %s: bad response" % (symbol, tf))
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("kline %s %s: no data" % (symbol, tf))
    parsed: Dict[int, Tuple[float, float, float, float, float, float]] = {}
    for r in rows_raw:
        try:
            ts = int(float(r[0]))
            if ts > 10 ** 11:
                ts //= 1000
            o, h, l, c, v = (float(x) for x in r[1:6])
        except (ValueError, TypeError, IndexError):
            continue
        if min(o, h, l, c) <= 0 or h < l or v < 0:
            continue
        parsed[ts] = (ts, o, h, l, c, v)
    rows = [parsed[k] for k in sorted(parsed)]
    if rows and rows[-1][0] + sec > time.time():
        rows.pop()
    if len(rows) < MIN_CANDLES:
        raise ScanError("kline %s %s: only %d candles" % (symbol, tf, len(rows)))
    return Candles(rows)


def fetch_price(symbol: str) -> Optional[float]:
    try:
        data = http_json(LBANK_BASE + "/v2/ticker/24hr.do", {"symbol": symbol.lower()})
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            tk = items[0].get("ticker", {})
            p = float(tk.get("latest"))
            if p > 0:
                return p
    except (ScanError, TypeError, ValueError, AttributeError):
        pass
    return None


def fetch_pairs() -> Optional[set]:
    try:
        data = http_json(LBANK_BASE + "/v2/currencyPairs.do")
    except ScanError:
        return None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    pairs = {str(x).lower() for x in items if isinstance(x, str)}
    return pairs if "btc_usdt" in pairs else None


# --------------------------------------------------------------------------
# Technical & Structure Primitives
# --------------------------------------------------------------------------
def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[float]:
    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out: List[float] = []
    a = 0.0
    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            a = sum(trs[:n]) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


def volume_ratio(v: List[float]) -> float:
    if len(v) < 24:
        return 1.0
    recent = sum(v[-3:]) / 3.0
    base = sum(v[-23:-3]) / 20.0
    return recent / base if base > 0 else 1.0


def find_swings(
    h: List[float], l: List[float], n: int = 3
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and all(
            h[i] >= h[i + k] for k in range(1, n + 1)
        ):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and all(
            l[i] <= l[i + k] for k in range(1, n + 1)
        ):
            lows.append((i, l[i]))
    return highs, lows


def structure_dir(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> int:
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


def structure_trend(
    c: List[float], highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]
) -> str:
    if not highs or not lows:
        return "RANGE"
    st = structure_dir(highs, lows)
    last = c[-1]
    if st == 1:
        return "RANGE" if last < lows[-1][1] else "UP"
    if st == -1:
        return "RANGE" if last > highs[-1][1] else "DOWN"
    if last > highs[-1][1]:
        return "UP"
    if last < lows[-1][1]:
        return "DOWN"
    return "RANGE"


def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    c, h, l = cs.c, cs.h, cs.l
    at = atr(h, l, c)
    n_swing = 2 if name in ("4h", "5m") else 3
    highs, lows = find_swings(h, l, n_swing
