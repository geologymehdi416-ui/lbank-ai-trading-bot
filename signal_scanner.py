#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Analysis-only crypto futures signal scanner (NO order execution).

Pipeline per symbol:
  1. Fetch price + 5m / 15m / 1H / 4H candles from LBank (public API).
  2. Higher-timeframe STRUCTURE trend (HH/HL, LH/LL, CHoCH); ATR and volume
     are used only as market filters. No EMA/RSI/MACD (pure price action).
  3. Swings, support/resistance, liquidity pools, liquidity sweep, MSS/BOS,
     FVG and Order Blocks.
  4. Score LONG and SHORT scenarios out of 100.
  5. Build Entry / SL / TP1 / TP2 / RR; signal only if score >= MIN_SCORE and
     all risk gates pass.
  6. Send scan report + signals to Telegram.

Environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram delivery (if missing -> stdout)
  SYMBOLS            comma list, default 15 liquid majors/alts (see DEFAULT_SYMBOLS)
  MIN_SCORE          default 75
  RISK_PCT           risk per trade in % of equity, default 0.5
  MAX_LEVERAGE       default 2
  ACCOUNT_EQUITY     optional (USDT) -> shows position size in the signal
  LBANK_BASE_URL     default https://api.lbkex.com
  REQUEST_INTERVAL   min seconds between API calls, default 0.25
  STATE_FILE         cooldown file, default signal_state.json
  SIGNAL_COOLDOWN_H  hours before the same symbol+side is re-sent, default 4

Usage:
  python signal_scanner.py
  python signal_scanner.py --symbols BTC_USDT,ETH_USDT --dry-run
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
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
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT,"
                   "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,"
                   "DOT_USDT,LTC_USDT,ATOM_USDT,NEAR_USDT,BCH_USDT,"
                   "TRX_USDT,HBAR_USDT,ICP_USDT,FIL_USDT,ETC_USDT,XLM_USDT,ALGO_USDT,AGT_USDT,AKE_USDT")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 75.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
COOLDOWN_H = _env_float("SIGNAL_COOLDOWN_H", 4.0)
STATE_FILE = os.getenv("STATE_FILE", "signal_state.json")
SCAN_LOG_FILE = os.getenv("SCAN_LOG_FILE", "scan_log.csv")   # empty string disables logging
DIAG_FILE = os.getenv("DIAG_FILE", "diag_report.txt")        # per-gate diagnostic report (never sent to Telegram)

INDICATOR_FILTER = os.getenv("INDICATOR_FILTER", "0").strip() == "1"  # 1 = indicator check can block signals; 0 = diagnostics only
FIB_CONFIRM = os.getenv("FIB_CONFIRM", "0").strip() == "1"   # 1 = Fibonacci affects score/TP; 0 = diagnostics only
SWING_MIN_AMP = _env_float("SWING_MIN_AMP", 0.0)              # 0 = off; else a swing needs a move >= this x ATR
FIB_EXT_LEVELS = (1.272, 1.618)   # extension targets measured from the pullback point
FIB_PTS_OVERLAP = 5               # fib pullback zone overlaps an OB/FVG and price is in it
FIB_PTS_ZONE = 2                  # price inside the fib pullback zone only

MAX_STOP_PCT = 3.0          # max stop distance, % of entry
MAX_STOP_ATR = 3.0          # max stop distance in 15m ATR
MIN_STOP_ATR = 0.5          # min stop distance in 15m ATR
MIN_ATR_PCT = 0.0004        # 15m ATR / price below this -> dead market
MAX_ATR_PCT = 0.03          # above this -> too volatile
MIN_VOL_RATIO = 0.5
MIN_CANDLES = 100

MIN_STOP_PCT = 0.25         # minimum stop distance, % of entry (stop is widened to this)
FEE_PCT = _env_float("FEE_PCT", 0.06)   # taker fee per side, % (set to your real fee)
CONFIRM_VOL = _env_float("CONFIRM_VOL", 1.2)     # 1m confirmation: volume >= this x avg
CONFIRM_BODY = _env_float("CONFIRM_BODY", 0.5)   # 1m confirmation: body >= this x avg body
MIN_RR2_NET = 1.5           # min reward/risk to TP2 after round-trip fees (signal gate)
MAX_TP_RR = _env_float("MAX_TP_RR", 5.0)
CONDITIONAL_MIN_RR = _env_float("CONDITIONAL_MIN_RR", 1.0)  # TP1 net RR needed to show a conditional plan as waiting  # targets beyond this net RR are unrealistic for the window and skipped
EXPIRY_H = _env_float("EXPIRY_H", 4.0)  # hours a conditional opportunity stays valid
TP_MIN_RR_NET = _env_float("TP_MIN_RR_NET", 0.5)  # each structural target must have net RR >= this
WATCH_MIN = 60              # minimum score for the Watchlist / conditional opportunities
CARD_MIN_RR = _env_float("CARD_MIN_RR", 0.0)  # display only: hide cards whose TP1 net RR is below this (0 = show all)
STRUCT_CAP = 60             # max score when Sweep+MSS/BOS is not confirmed
MAX_SAME_DIR = 2            # max signals in the same direction per scan
MSS_MAX_DELAY = 8           # MSS must happen within N candles after the sweep
MSS_MIN_BODY = 0.8          # MSS candle body >= this x ATR
MIN_ATR_RANK = 0.10         # 15m ATR percentile (last 100 candles) below this = quiet market
QUIET_RANGE_RANK = 0.10     # 20-candle 15m range percentile (vs last ~200 windows) below this = compression
BREAKOUT_WINDOW = 20        # candles that define the compression range
BREAKOUT_MAX_RANK = 0.30    # the range before a breakout must be in the lowest 30% of this symbol's own ranges
BREAKOUT_MIN_VOL = 1.2      # breakout candle volume >= this x mean volume of the range

# tf name -> (LBank type, seconds)
TF_MAP: Dict[str, Tuple[str, int]] = {
    "1m": ("minute1", 60),
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

TREND_FA = {"UP": "صعودی", "DOWN": "نزولی", "RANGE": "رنج"}
TREND_ARROW = {"UP": "↑", "DOWN": "↓", "RANGE": "↔"}
DECISION_FA = {"ENTER": "ورود", "CONDITIONAL": "شرطی (هنوز ورود نکن)", "NO_ENTRY": "ورود نکن"}
REGIME_FA = {"DEAD": "مرده", "QUIET": "فشرده", "NORMAL": "عادی", "WILD": "پرنوسان"}


class ScanError(Exception):
    """Recoverable error for a single symbol / request."""


# --------------------------------------------------------------------------
# HTTP with throttling, retries and backoff
# --------------------------------------------------------------------------
_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def http_json(url: str, params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None,
              retries: int = 4, timeout: float = 15.0) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"User-Agent": "signal-scanner/1.0", "Accept": "application/json"}
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
                delay = float(2 ** attempt)
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra:
                    try:
                        delay = max(delay, float(ra))
                    except ValueError:
                        pass
                log.warning("%s - retry %d/%d in %.0fs", last_err, attempt, retries, min(delay, 30))
                time.sleep(min(delay, 30))
                continue
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            raise ScanError("%s %s" % (last_err, detail))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network: %s" % (getattr(e, "reason", None) or e)
            log.warning("%s - retry %d/%d", last_err, attempt, retries)
            time.sleep(min(2 ** attempt, 20))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON response"
            log.warning("%s - retry %d/%d", last_err, attempt, retries)
            time.sleep(min(2 ** attempt, 20))
    raise ScanError("request failed after %d tries (%s)" % (retries, last_err))


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
class Candles:
    """Column-oriented OHLCV container (closed candles only)."""

    def __init__(self, rows: List[Tuple[float, float, float, float, float, float]]):
        self.t = [r[0] for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] for r in rows]

    def __len__(self) -> int:
        return len(self.c)


def fetch_klines(symbol: str, tf: str, size: int = 300) -> Candles:
    typ, sec = TF_MAP[tf]
    now = int(time.time())
    start = now - size * sec
    data = http_json(LBANK_BASE + "/v2/kline.do",
                     {"symbol": symbol.lower(), "size": size, "type": typ, "time": start})
    if not isinstance(data, dict) or str(data.get("result", "")).lower() != "true":
        raise ScanError("kline %s %s: bad response %s" % (symbol, tf, str(data)[:120]))
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("kline %s %s: no data" % (symbol, tf))
    parsed: Dict[int, Tuple[float, float, float, float, float, float]] = {}
    for r in rows_raw:
        try:
            ts = int(float(r[0]))
            if ts > 10 ** 11:          # milliseconds -> seconds
                ts //= 1000
            o, h, l, c, v = (float(x) for x in r[1:6])
        except (ValueError, TypeError, IndexError):
            continue
        if min(o, h, l, c) <= 0 or h < l or v < 0:
            continue
        parsed[ts] = (ts, o, h, l, c, v)
    rows = [parsed[k] for k in sorted(parsed)]
    if rows and rows[-1][0] + sec > time.time():   # drop the still-forming candle
        rows.pop()
    if len(rows) < MIN_CANDLES:
        raise ScanError("kline %s %s: only %d candles" % (symbol, tf, len(rows)))
    age = time.time() - (rows[-1][0] + sec)
    if age > 2 * sec + 120:
        raise ScanError("kline %s %s: stale data (%.0f min old)" % (symbol, tf, age / 60))
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
    except (ScanError, TypeError, ValueError, AttributeError) as e:
        log.warning("ticker %s failed: %s", symbol, e)
    return None


def fetch_pairs() -> Optional[set]:
    """Best-effort list of LBank spot pairs; None if unavailable (then no filtering)."""
    try:
        data = http_json(LBANK_BASE + "/v2/currencyPairs.do")
    except ScanError as e:
        log.warning("pair list unavailable: %s", e)
        return None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    pairs = {str(x).lower() for x in items if isinstance(x, str)}
    return pairs if "btc_usdt" in pairs else None


# --------------------------------------------------------------------------
# ATR / volume (filters only)
# --------------------------------------------------------------------------
def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[float]:
    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out: List[float] = []
    a = 0.0
    for i, tr in enumerate(trs):
        if i < n - 1:
