#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Analysis-only crypto futures signal scanner (NO order execution).
Combines Liquidity Hunt (SSL/BSL sweep + MSS/BOS) + OB/FVG + Fibonacci (0.618-0.786)
with chart-aligned Entry, Stop Loss, clear Targets (TP1/TP2/TP3), and configurable leverage.
"""
from __future__ import annotations

import argparse
import csv
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
COOLDOWN_H = _env_float("SIGNAL_COOLDOWN_H", 4.0)
STATE_FILE = os.getenv("STATE_FILE", "signal_state.json")
SCAN_LOG_FILE = os.getenv("SCAN_LOG_FILE", "scan_log.csv")
DIAG_FILE = os.getenv("DIAG_FILE", "diag_report.txt")

INDICATOR_FILTER = os.getenv("INDICATOR_FILTER", "0").strip() == "1"
FIB_CONFIRM = os.getenv("FIB_CONFIRM", "1").strip() == "1"
REQUIRE_SWEEP_OR_MSS = os.getenv("REQUIRE_SWEEP_OR_MSS", "1").strip() == "1"
SWING_MIN_AMP = _env_float("SWING_MIN_AMP", 0.0)
FIB_EXT_LEVELS = (1.272, 1.618)
FIB_PTS_OVERLAP = 5
FIB_PTS_ZONE = 2

MAX_STOP_PCT = 3.0
MAX_STOP_ATR = 3.0
MIN_STOP_ATR = 0.5
MIN_ATR_PCT = 0.0004
MAX_ATR_PCT = 0.03
MIN_VOL_RATIO = 0.5
MIN_CANDLES = 100

MIN_STOP_PCT = 0.25
FEE_PCT = _env_float("FEE_PCT", 0.06)
CONFIRM_VOL = _env_float("CONFIRM_VOL", 1.2)
CONFIRM_BODY = _env_float("CONFIRM_BODY", 0.5)
MIN_RR1_NET = _env_float("MIN_RR1_NET", 1.0)
MIN_RR2_NET = 1.5
MAX_TP_RR = _env_float("MAX_TP_RR", 10.0)
CONDITIONAL_MIN_RR = _env_float("CONDITIONAL_MIN_RR", 1.0)
EXPIRY_H = _env_float("EXPIRY_H", 4.0)
TP_MIN_RR_NET = _env_float("TP_MIN_RR_NET", 0.6)
WATCH_MIN = 60
CARD_MIN_RR = _env_float("CARD_MIN_RR", 0.0)
STRUCT_CAP = 60
MAX_SAME_DIR = 2
MSS_MAX_DELAY = 8
MSS_MIN_BODY = 0.8
MIN_ATR_RANK = 0.10
QUIET_RANGE_RANK = 0.10
BREAKOUT_WINDOW = 20
BREAKOUT_MAX_RANK = 0.30
BREAKOUT_MIN_VOL = 1.2

CAPITAL_USDT = _env_float("ACCOUNT_EQUITY_USDT", 1000.0)
CAPITAL_RISK_PCT = _env_float("CAPITAL_RISK_PCT", 1.0)

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
    data = http_json(
        LBANK_BASE + "/v2/kline.do",
        {"symbol": symbol.lower(), "size": size, "type": typ, "time": start},
    )
    if not isinstance(data, dict) or str(data.get("result", "")).lower() != "true":
        raise ScanError("kline %s %s: bad response %s" % (symbol, tf, str(data)[:120]))
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
    if base <= 0:
        return 1.0
    return recent / base


# --------------------------------------------------------------------------
# Structure helpers
# --------------------------------------------------------------------------
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


def filter_swings_amp(
    h: List[float], l: List[float], highs, lows, at: List[float], min_amp: float
):
    if min_amp <= 0:
        return highs, lows

    def amp_ok(i: int, price: float, is_high: bool) -> bool:
        a = at[i] if i < len(at) else at[-1]
        for j in range(i + 1, min(len(h), i + 11)):
            if is_high and price - min(l[i:j + 1]) >= min_amp * a:
                return True
            if (not is_high) and max(h[i:j + 1]) - price >= min_amp * a:
                return True
        return False

    return (
        [p for p in highs if amp_ok(p[0], p[1], True)],
        [p for p in lows if amp_ok(p[0], p[1], False)],
    )


def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    c, h, l = cs.c, cs.h, cs.l
    at = atr(h, l, c)
    highs, lows = find_swings(h, l, 2 if name == "4h" else 3)
    highs, lows = filter_swings_amp(h, l, highs, lows, at, SWING_MIN_AMP)
    st = structure_dir(highs, lows)
    recent_atr = at[-100:]
    atr_rank = sum(1 for x in recent_atr if x <= at[-1]) / float(len(recent_atr))
    trend = structure_trend(c, highs, lows)
    return {
        "name": name,
        "cs": cs,
        "close": c[-1],
        "atr": max(at[-1], c[-1] * 1e-6),
        "vol_ratio": volume_ratio(cs.v),
        "highs": highs,
        "lows": lows,
        "struct": st,
        "trend": trend,
        "atr_rank": atr_rank,
        "atr_list": at,
    }


# --------------------------------------------------------------------------
# Smart-money & Liquidity Hunt
# --------------------------------------------------------------------------
def detect_sweep(d: Dict[str, Any], side: str, lookback: int) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    a = d["atr"]
    long = side == "LONG"
    pts = [p for p in (d["lows"] if long else d["highs"]) if p[0] >= n - 150]
    for j in range(n - 1, max(n - 1 - lookback, 2), -1):
        for si, lvl in reversed(pts):
            if si >= j - 1:
                continue
            if long:
                ok = (
                    cs.l[j] < lvl - 0.03 * a
                    and cs.c[j] > lvl
                    and all(x >= lvl for x in cs.c[si + 1:j])
                )
                ext = min(cs.l[j:]) if ok else 0.0
            else:
                ok = (
                    cs.h[j] > lvl + 0.03 * a
                    and cs.c[j] < lvl
                    and all(x <= lvl for x in cs.c[si + 1:j])
                )
                ext = max(cs.h[j:]) if ok else 0.0
            if ok:
                is_eq = sum(1 for _, p in pts if abs(p - lvl) <= 0.15 * a) >= 2
                return {
                    "idx": j,
                    "level": lvl,
                    "extreme": ext,
                    "age": n - 1 - j,
                    "tf": d["name"],
                    "eq_pool": is_eq,
                }
    return None


def detect_mss(
    d: Dict[str, Any], side: str, sweep: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    a = d["atr"]
    j = sweep["idx"]
    end = min(len(cs), j + MSS_MAX_DELAY + 1)
    if side == "LONG":
        prior = [p for p in d["highs"] if p[0] < j]
        if not prior:
            return None
        lvl = prior[-1][1]
        for k in range(j, end):
            if cs.c[k] > lvl and (cs.c[k] - cs.o[k]) >= MSS_MIN_BODY * a:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    else:
        prior = [p for p in d["lows"] if p[0] < j]
        if not prior:
            return None
        lvl = prior[-1][1]
        for k in range(j, end):
            if cs.c[k] < lvl and (cs.o[k] - cs.c[k]) >= MSS_MIN_BODY * a:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    return None


def detect_bos(d: Dict[str, Any], side: str, within: int = 6) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    if side == "LONG":
        prior = [p for p in d["highs"] if p[0] < n - within]
        if prior and max(cs.c[-within:]) > prior[-1][1]:
            return {"level": prior[-1][1], "tf": d["name"]}
    else:
        prior = [p for p in d["lows"] if p[0] < n - within]
        if prior and min(cs.c[-within:]) < prior[-1][1]:
            return {"level": prior[-1][1], "tf": d["name"]}
    return None


def find_fvgs(
    cs: Candles, atr_v: float, side: str, lookback: int = 50
) -> List[Dict[str, float]]:
    n = len(cs)
    out: List[Dict[str, float]] = []
    for i in range(max(2, n - lookback), n):
        if side == "LONG":
            lo, hi = cs.h[i - 2], cs.l[i]
            if hi > lo and hi - lo >= 0.1 * atr_v:
                later = min(cs.l[i + 1:]) if i + 1 < n else float("inf")
                if later > lo:
                    out.append({"bottom": lo, "top": hi})
        else:
            hi, lo = cs.l[i - 2], cs.h[i]
            if hi > lo and hi - lo >= 0.1 * atr_v:
                later = max(cs.h[i + 1:]) if i + 1 < n else float("-inf")
                if later < hi:
                    out.append({"bottom": lo, "top": hi})
    return out


def find_obs(
    cs: Candles, atr_v: float, side: str, lookback: int = 60
) -> List[Dict[str, float]]:
    n = len(cs)
    out: List[Dict[str, float]] = []
    for i in range(max(1, n - lookback), n - 3):
        if side == "LONG" and cs.c[i] < cs.o[i]:
            move = max(cs.h[i + 1:i + 4]) - cs.h[i]
            if move >= 0.8 * atr_v and min(cs.c[i + 1:]) > cs.l[i]:
                out.append({"bottom": cs.l[i], "top": cs.o[i]})
        elif side == "SHORT" and cs.c[i] > cs.o[i]:
            move = cs.l[i] - min(cs.l[i + 1:i + 4])
            if move >= 0.8 * atr_v and max(cs.c[i + 1:]) < cs.h[i]:
                out.append({"bottom": cs.o[i], "top": cs.h[i]})
    return out


def _zones_for(d: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    zs: List[Dict[str, Any]] = []
    for z in find_fvgs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "FVG", "tf": d["name"]})
    for z in find_obs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "OB", "tf": d["name"]})
    return zs


def pick_zone(
    ds: List[Dict[str, Any]], side: str, price: float, atr15: float
) -> Optional[Dict[str, Any]]:
    long = side == "LONG"
    allz: List[Dict[str, Any]] = []
    for d in ds:
        allz.extend(_zones_for(d, side))

    def dist(z: Dict[str, Any]) -> float:
        return max(0.0, price - z["top"]) if long else max(0.0, z["bottom"] - price)

    def valid(z: Dict[str, Any]) -> bool:
        if z["top"] <= z["bottom"]:
            return False
        if long:
            return z["top"] <= price + 0.1 * atr15 and z["bottom"] < price and dist(z) <= 1.5 * atr15
        return z["bottom"] >= price - 0.1 * atr15 and z["top"] > price and dist(z) <= 1.5 * atr15

    cands: List[Tuple[int, float, Dict[str, Any]]] = []
    fvgs = [z for z in allz if z["kind"] == "FVG"]
    obs = [z for z in allz if z["kind"] == "OB"]
    for f in fvgs:
        for o in obs:
            lo, hi = max(f["bottom"], o["bottom"]), min(f["top"], o["top"])
            if hi > lo:
                z = {"bottom": lo, "top": hi, "kind": "OB+FVG", "tf": f["tf"]}
                if valid(z):
                    cands.append((0, dist(z), z))
    for z in allz:
        if valid(z):
            cands.append((1, dist(z), z))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def collect_levels(D: Dict[str, Dict[str, Any]], htf_only: bool = False) -> List[float]:
    names = ("1h", "4h") if htf_only else ("15m", "1h", "4h")
    lv: List[float] = []
    for nm in names:
        d = D[nm]
        lv.extend(p for _, p in d["highs"])
        lv.extend(p for _, p in d["lows"])
    return sorted(set(lv))


def liquidity_map(d: Dict[str, Any], price: float) -> Dict[str, Any]:
    tol = 0.15 * d["atr"]
    above = [p for _, p in d["highs"] if p > price]
    below = [p for _, p in d["lows"] if p < price]
    out: Dict[str, Any] = {"bsl": None, "bsl_eq": False, "ssl": None, "ssl_eq": False}
    if above:
        b = min(above)
        out["bsl"] = b
        out["bsl_eq"] = sum(1 for _, p in d["highs"] if abs(p - b) <= tol) >= 2
    if below:
        s = max(below)
        out["ssl"] = s
        out["ssl_eq"] = sum(1 for _, p in d["lows"] if abs(p - s) <= tol) >= 2
    return out


# --------------------------------------------------------------------------
# Regime & indicators
# --------------------------------------------------------------------------
def squeeze_info(d: Dict[str, Any], n: int = BREAKOUT_WINDOW) -> Dict[str, float]:
    cs: Candles = d["cs"]
    total = len(cs)
    hi, lo = max(cs.h[-n:]), min(cs.l[-n:])
    cur = hi - lo
    start = max(n, total - 200)
    ranges = [max(cs.h[i - n:i]) - min(cs.l[i - n:i]) for i in range(start, total + 1)]
    rank = sum(1 for x in ranges if x <= cur) / float(len(ranges))
    return {"hi": hi, "lo": lo, "range_atr": cur / d["atr"], "range_rank": rank}


def detect_breakout(d: Dict[str, Any], within: int = 3) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    at: List[float] = d["atr_list"]
    n = len(cs)
    w = BREAKOUT_WINDOW
    ranges: Dict[int, float] = {}
    for j in range(max(w, n - 1 - within - 200), n):
        ranges[j] = max(cs.h[j - w:j]) - min(cs.l[j - w:j])
    for k in range(n - 1, n - 1 - within, -1):
        if k - w < 1 or k not in ranges:
            break
        hi, lo = max(cs.h[k - w:k]), min(cs.l[k - w:k])
        a = at[k - 1]
        if a <= 0:
            continue
        hist = [v for j, v in ranges.items() if j <= k]
        rank = sum(1 for v in hist if v <= ranges[k]) / float(len(hist))
        if rank > BREAKOUT_MAX_RANK:
            continue
        vmean = sum(cs.v[k - w:k]) / float(w)
        if vmean <= 0 or cs.v[k] < BREAKOUT_MIN_VOL * vmean:
            continue
        body = cs.c[k] - cs.o[k]
        if cs.c[k] > hi and body >= MSS_MIN_BODY * a and cs.c[-1] > hi:
            return {"dir": "UP", "idx": k, "level": hi, "range_hi": hi, "range_lo": lo}
        if cs.c[k] < lo and -body >= MSS_MIN_BODY * a and cs.c[-1] < lo:
            return {"dir": "DOWN", "idx": k, "level": lo, "range_hi": hi, "range_lo": lo}
    return None


def classify_regime(D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    d15 = D["15m"]
    atr_pct = d15["atr"] / price
    sq = squeeze_info(d15)
    bo = detect_breakout(d15)
    quiet = d15["atr_rank"] < MIN_ATR_RANK or sq["range_rank"] < QUIET_RANGE_RANK
    if atr_pct < MIN_ATR_PCT:
        name = "DEAD"
    elif atr_pct > MAX_ATR_PCT:
        name = "WILD"
    elif quiet:
        name = "QUIET"
    else:
        name = "NORMAL"
    return {"name": name, "breakout": bo, "squeeze": sq, "atr_pct": atr_pct}


def global_blockers(
    D: Dict[str, Dict[str, Any]], price: float, regime: Dict[str, Any]
) -> List[str]:
    out: List[str] = []
    d15, d5 = D["15m"], D["5m"]
    if regime["name"] == "DEAD":
        out.append("نوسان بازار خیلی کم است (بازار مرده)")
    elif regime["name"] == "WILD":
        out.append("نوسان بازار بیش‌ازحد زیاد است")
    if d15["vol_ratio"] < MIN_VOL_RATIO and not regime["breakout"]:
        out.append("حجم معاملات ضعیف است")
    cs5: Candles = d5["cs"]
    cs15: Candles = d15["cs"]
    if (cs5.h[-1] - cs5.l[-1]) > 4.0 * d5["atr"] or (cs15.h[-1] - cs15.l[-1]) > 4.0 * d15["atr"]:
        out.append("کندل اسپایک اخیر؛ صبر برای آرام‌شدن بازار")
    return out


def _ema(values: List[float], n: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def _rsi_last(closes: List[float], n: int = 14) -> float:
    if len(closes) <= n:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _macd_hist_pair(closes: List[float]) -> Tuple[float, float]:
    ef, es = _ema(closes, 12), _ema(closes, 26)
    line = [a - b for a, b in zip(ef, es)]
    sig = _ema(line, 9)
    hist = [a - b for a, b in zip(line, sig)]
    return hist[-1], hist[-2] if len(hist) > 1 else hist[-1]


def indicator_info(D: Dict[str, Dict[str, Any]], side: str) -> Dict[str, Any]:
    long = side == "LONG"
    c15 = D["15m"]["cs"].c
    c1 = D["1h"]["cs"].c
    e20 = _ema(c15, 20)[-1]
    e50 = _ema(c15, 50)[-1]
    rsi = _rsi_last(c15)
    h_now, h_prev = _macd_hist_pair(c15)
    e1_50 = _ema(c1, 50)[-1]
    checks = {
        "EMA15": (c15[-1] > e50 and e20 > e50) if long else (c15[-1] < e50 and e20 < e50),
        "RSI": (45 <= rsi <= 70) if long else (30 <= rsi <= 55),
        "MACD": (h_now > 0 and h_now >= h_prev) if long else (h_now < 0 and h_now <= h_prev),
        "EMA1H": (c1[-1] > e1_50) if long else (c1[-1] < e1_50),
    }
    passed = sum(1 for v in checks.values() if v)
    failed = [k for k, v in checks.items() if not v]
    return {
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "failed": failed,
        "ok": passed >= 3,
        "rsi": rsi,
        "ema20": e20,
        "ema50": e50,
    }


def premium_discount(D: Dict[str, Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    d1 = D["1h"]
    if not d1["highs"] or not d1["lows"]:
        return None
    hi, lo = d1["highs"][-1][1], d1["lows"][-1][1]
    if hi <= lo:
        return None
    pos = (price - lo) / (hi - lo) * 100.0
    zone = "پریمیوم (بالا)" if pos > 50 else "دیسکانت (پایین)"
    return {"pos": pos, "zone": zone, "eq": (hi + lo) / 2.0}


# --------------------------------------------------------------------------
# Fibonacci & Liquidity Hunt Engine (Chart-Aligned)
# --------------------------------------------------------------------------
FIB_MIN_IMPULSE_ATR = _env_float("MIN_IMPULSE_ATR", 1.5)
FIB_RECENT_BARS = int(_env_float("RECENT_BARS", 120))
FIB_SL_BUF_ATR = 0.25
PLAN_FILE = os.getenv("PLAN_FILE", "plans_state.json")
PLAN_TTL_H = _env_float("PLAN_TTL_H", 12.0)


def recent_impulse(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    n = len(d["cs"])
    lo_bar = n - FIB_RECENT_BARS
    hs = [h for h in d["highs"] if h[0] >= lo_bar]
    ls = [l for l in d["lows"] if l[0] >= lo_bar]
    if not hs or not ls:
        return None
    hi_i, hi_v = hs[-1]
    lo_i, lo_v = ls[-1]
    if lo_i > hi_i:
        A, B, direction = hi_v, lo_v, "DOWN"
    else:
        A, B, direction = lo_v, hi_v, "UP"
    diff = abs(A - B)
    if diff < FIB_MIN_IMPULSE_ATR * d["atr"]:
        return None
    return {"dir": direction, "A": A, "B": B, "diff": diff, "tf": d["name"]}


def fib_info(D: Dict[str, Dict[str, Any]], side: str, price: float) -> Dict[str, Any]:
    long = side == "LONG"
    out: Dict[str, Any] = {
        "valid": False,
        "retr": None,
        "zone": None,
        "targets": [],
        "overlap": False,
        "in_zone": False,
        "pts": 0,
    }
    d1, d15, d5 = D["1h"], D["15m"], D["5m"]
    if long:
        if not d1["highs"] or not d1["lows"]:
            return out
        bi, bp = d1["highs"][-1]
        prev = [p for p in d1["lows"] if p[0] < bi]
        after = [p for p in d1["lows"] if p[0] > bi]
        if not prev or not after:
            return out
        ap, cp = prev[-1][1], after[-1][1]
        diff = bp - ap
        if diff <= 0:
            return out
        retr = (bp - cp) / diff
        zone = (bp - 0.786 * diff, bp - 0.618 * diff)
        targets = [(k, cp + k * diff) for k in FIB_EXT_LEVELS if cp + k * diff > price]
    else:
        if not d1["lows"] or not d1["highs"]:
            return out
        bi, bp = d1["lows"][-1]
        prev = [p for p in d1["highs"] if p[0] < bi]
        after = [p for p in d1["highs"] if p[0] > bi]
        if not prev or not after:
            return out
        ap, cp = prev[-1][1], after[-1][1]
        diff = ap - bp
        if diff <= 0:
            return out
        retr = (cp - bp) / diff
        zone = (bp + 0.618 * diff, bp + 0.786 * diff)
        targets = [(k, cp - k * diff) for k in FIB_EXT_LEVELS if cp - k * diff < price]
    zlo, zhi = zone
    valid = 0.3 <= retr <= 0.9
    atr15 = d15["atr"]
    overlap = any(
        max(z["bottom"], zlo) < min(z["top"], zhi)
        for z in _zones_for(d15, side) + _zones_for(d5, side)
    )
    in_zone = (zlo - 0.25 * atr15) <= price <= (zhi + 0.25 * atr15)
    pts = 0
    if valid and in_zone:
        pts = FIB_PTS_OVERLAP if overlap else FIB_PTS_ZONE
    out.update({
        "valid": valid,
        "retr": retr,
        "zone": zone,
        "targets": targets,
        "overlap": overlap,
        "in_zone": in_zone,
        "pts": pts,
        "impulse": (ap, bp, cp, diff),
    })
    return out


def _chart_aligned_entry_and_sl(
    D: Dict[str, Dict[str, Any]], side: str, imp: Dict[str, Any], price: float
) -> Optional[Tuple[float, float, str]]:
    """Aligns entry with the OTE/Fibonacci level AND nearby OB/FVG/liquidity level
    so that the entry actually makes sense on the chart."""
    A, B, d = imp["A"], imp["B"], imp["diff"]
    atr15 = D["15m"]["atr"]
    if side == "SHORT":
        fib_entry = B + 0.618 * d
        fib_sl = B + 0.786 * d + FIB_SL_BUF_ATR * atr15
        zone = pick_zone([D["15m"], D["5m"]], "SHORT", price, atr15)
        if zone:
            z_mid = (zone["bottom"] + zone["top"]) / 2.0
            # Keep entry close to fib_entry if inside/near OB/FVG, else use chart zone/fib
            if abs(z_mid - fib_entry) <= 0.8 * atr15:
                entry = (fib_entry + z_mid) / 2.0
                src = "هم‌پوشانی فیبو ۰.۶۱۸ + %s" % zone["kind"]
            else:
                entry = fib_entry
                src = "فیبوناچی ۰.۶۱۸ (ناحیه عرضه)"
            sl = max(fib_sl, zone["top"] + FIB_SL_BUF_ATR * atr15)
        else:
            entry = fib_entry
            sl = fib_sl
            src = "فیبوناچی ۰.۶۱۸"
        if sl <= entry or price > sl:
            return None
        return entry, sl, src

    fib_entry = B - 0.618 * d
    fib_sl = B - 0.786 * d - FIB_SL_BUF_ATR * atr15
    zone = pick_zone([D["15m"], D["5m"]], "LONG", price, atr15)
    if zone:
        z_mid = (zone["bottom"] + zone["top"]) / 2.0
        if abs(z_mid - fib_entry) <= 0.8 * atr15:
            entry = (fib_entry + z_mid) / 2.0
            src = "هم‌پوشانی فیبو ۰.۶۱۸ + %s" % zone["kind"]
        else:
            entry = fib_entry
            src = "فیبوناچی ۰.۶۱۸ (ناحیه تقاضا)"
        sl = min(fib_sl, zone["bottom"] - FIB_SL_BUF_ATR * atr15)
    else:
        entry = fib_entry
        sl = fib_sl
        src = "فیبوناچی ۰.۶۱۸"
    if sl >= entry or price < sl:
        return None
    return entry, sl, src


def _build_clear_targets(
    D: Dict[str, Dict[str, Any]],
    side: str,
    imp: Dict[str, Any],
    entry: float,
    sl: float,
) -> List[Tuple[str, float, float]]:
    """Builds clear targets (TP1, TP2, TP3) combining chart structure/liquidity levels
    and Fibonacci extensions, filtered by net RR >= MIN_RR1_NET."""
    A, B, d = imp["A"], imp["B"], imp["diff"]
    risk_pct = abs(sl - entry) / entry * 100.0
    cost = 2.0 * FEE_PCT
    if risk_pct <= 0:
        return []

    def net_rr(t: float) -> float:
        valid_dir = (t > entry) if side == "LONG" else (t < entry)
        if not valid_dir:
            return 0.0
        dist_pct = abs(t - entry) / entry * 100.0
        return (dist_pct - cost) / (risk_pct + cost)

    raw_cands: List[Tuple[str, float]] = []
    if side == "LONG":
        raw_cands.append(("سقف/موج قبل (1.0)", B))
        raw_cands.append(("فیبو 1.272", A + 1.272 * d))
        raw_cands.append(("فیبو 1.618", A + 1.618 * d))
        sw_levels = [p for p in collect_levels(D) if p > entry]
        for lv in sorted(sw_levels):
            raw_cands.append(("نقدینگی/سقف", lv))
    else:
        raw_cands.append(("کف/موج قبل (1.0)", B))
        raw_cands.append(("فیبو 1.272", A - 1.272 * d))
        raw_cands.append(("فیبو 1.618", A - 1.618 * d))
        sw_levels = [p for p in collect_levels(D) if p < entry]
        for lv in sorted(sw_levels, reverse=True):
            raw_cands.append(("نقدینگی/کف", lv))

    min_gap_pct = max(0.35, 0.5 * risk_pct)
    selected: List[Tuple[str, float, float]] = []
    last_dist = 0.0

    for label, lvl in raw_cands:
        ratio = net_rr(lvl)
        if ratio < MIN_RR1_NET or ratio > MAX_TP_RR:
            continue
        dist_pct = abs(lvl - entry) / entry * 100.0
        if dist_pct < last_dist + min_gap_pct:
            continue
        selected.append((label, lvl, ratio))
        last_dist = dist_pct
        if len(selected) == 3:
            break

    return selected


def fib_point_plan(D: Dict[str, Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    t1, t4 = D["1h"]["trend"], D["4h"]["trend"]
    imp = None
    for tf in ("1h", "4h"):
        cand = recent_impulse(D[tf])
        if cand is None:
            continue
        agrees = (
            cand["dir"] == "UP"
            and t4 != "DOWN"
            and (t1 == "UP" or (t1 == "RANGE" and t4 == "UP"))
        ) or (
            cand["dir"] == "DOWN"
            and t4 != "UP"
            and (t1 == "DOWN" or (t1 == "RANGE" and t4 == "DOWN"))
        )
        if agrees:
            imp = cand
            break
    if imp is None:
        return None

    side = "SHORT" if imp["dir"] == "DOWN" else "LONG"
    pd_ = premium_discount(D, price)
    if side == "SHORT" and pd_ and pd_["pos"] < 35.0:
        return None
    if side == "LONG" and pd_ and pd_["pos"] > 65.0:
        return None

    sweep_obj = None
    mss_obj = None
    for dd in (D["15m"], D["5m"]):
        sw = detect_sweep(dd, side, 24)
        if sw:
            sweep_obj = sw
            m = detect_mss(dd, side, sw)
            if m:
                mss_obj = m
                break

    if REQUIRE_SWEEP_OR_MSS and not (sweep_obj or mss_obj):
        return None

    aligned = _chart_aligned_entry_and_sl(D, side, imp, price)
    if aligned is None:
        return None
    entry, sl, entry_src = aligned

    risk_pct = abs(sl - entry) / entry * 100.0
    if risk_pct <= 0 or risk_pct > MAX_STOP_PCT or abs(entry - price) / price > 0.08:
        return None

    targets = _build_clear_targets(D, side, imp, entry, sl)
    if not targets:
        return None

    tp1_lbl, tp1, rr1 = targets[0]
    tp2_lbl, tp2, rr2 = targets[1] if len(targets) > 1 else ("", None, None)
    tp3_lbl, tp3, rr3 = targets[2] if len(targets) > 2 else ("", None, None)

    if side == "SHORT":
        if price > sl:
            status = "قیمت از حد ضرر گذشته؛ منقضی"
        elif price >= entry:
            status = "در ناحیه‌ی ورود؛ منتظر کندل رد"
        else:
            status = "منتظر رسیدن قیمت به ورود (بالا)"
    else:
        if price < sl:
            status = "قیمت از حد ضرر گذشته؛ منقضی"
        elif price <= entry:
            status = "در ناحیه‌ی ورود؛ منتظر کندل رد"
        else:
            status = "منتظر رسیدن قیمت به ورود (پایین)"

    confluence = bool(sweep_obj and mss_obj)
    if confluence:
        tag = "تأیید کامل: Sweep + MSS"
    elif sweep_obj:
        tag = "دارای Sweep"
    else:
        tag = "دارای MSS"

    status = "%s | %s (%s)" % (status, tag, entry_src)
    return {
        "side": side,
        "tf": imp["tf"],
        "entry": entry,
        "entry_src": entry_src,
        "sl": sl,
        "risk_pct": risk_pct,
        "tp1": tp1,
        "tp1_lbl": tp1_lbl,
        "rr1": rr1,
        "tp2": tp2,
        "tp2_lbl": tp2_lbl,
        "rr2": rr2,
        "tp3": tp3,
        "tp3_lbl": tp3_lbl,
        "rr3": rr3,
        "dist_pct": (entry - price) / price * 100.0,
        "status": status,
        "confluence": confluence,
        "sweep": sweep_obj,
        "mss": mss_obj,
        "A": imp["A"],
        "B": imp["B"],
    }


def _plan_status(side: str, entry: float, sl: float, tp1: float, price: float) -> str:
    if side == "SHORT":
        if price > sl:
            return "قیمت از حد ضرر گذشته؛ منقضی"
        if price <= tp1:
            return "تارگت اول لمس شده؛ منقضی"
        return "در ناحیه‌ی ورود؛ منتظر کندل رد" if price >= entry else "منتظر رسیدن قیمت به ورود (بالا)"
    if price < sl:
        return "قیمت از حد ضرر گذشته؛ منقضی"
    if price >= tp1:
        return "تارگت اول لمس شده؛ منقضی"
    return "در ناحیه‌ی ورود؛ منتظر کندل رد" if price <= entry else "منتظر رسیدن قیمت به ورود (پایین)"


def _load_plans() -> Dict[str, Any]:
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_plans(plans: Dict[str, Any]) -> None:
    try:
        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("could not save plans: %s", e)


def stable_plan(
    symbol: str, plan: Optional[Dict[str, Any]], price: float
) -> Optional[Dict[str, Any]]:
    if plan is None:
        return None
    now = time.time()
    plans = _load_plans()
    old = plans.get(symbol)
    if (
        old
        and old.get("side") == plan["side"]
        and now < old.get("until", 0)
        and _plan_status(old["side"], old["entry"], old["sl"], old["tp1"], price).find("منقضی") < 0
    ):
        keep = dict(old)
        keep["status"] = _plan_status(old["side"], old["entry"], old["sl"], old["tp1"], price)
        keep["dist_pct"] = (old["entry"] - price) / price * 100.0
        keep["stored"] = True
        return keep
    fresh = dict(plan)
    fresh["until"] = now + PLAN_TTL_H * 3600.0
    fresh["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fresh["stored"] = False
    if "منقضی" in fresh["status"]:
        fresh["status"] = fresh["status"].split(" | ")[0]
    if "منقضی" not in _plan_status(plan["side"], plan["entry"], plan["sl"], plan["tp1"], price):
        plans[symbol] = fresh
        _save_plans(plans)
    return fresh


# --------------------------------------------------------------------------
# Side evaluation & formatting
# --------------------------------------------------------------------------
def pick_targets(
    levels: List[float], entry: float, risk: float, long: bool, fee_pct: float
) -> List[Tuple[float, float]]:
    cost = 2.0 * fee_pct
    risk_pct = risk / entry * 100.0
    pool = sorted([p for p in levels if (p > entry if long else p < entry)], reverse=not long)
    out: List[Tuple[float, float]] = []
    last = 0.0
    for p in pool:
        dist = abs(p - entry) / entry * 100.0
        rr = (dist - cost) / (risk_pct + cost)
        min_gap = 0.5 * risk_pct if out else 0.0
        if rr < TP_MIN_RR_NET or rr > MAX_TP_RR or dist <= last or dist < last + min_gap:
            continue
        out.append((p, rr))
        last = dist
        if len(out) == 2:
            break
    return out


def evaluate_side(side: str, D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    long = side == "LONG"
    want, opp = ("UP", "DOWN") if long else ("DOWN", "UP")
    d4, d1, d15, d5 = D["4h"], D["1h"], D["15m"], D["5m"]
    atr15 = d15["atr"]
    notes: List[str] = []
    blockers: List[str] = []
    soft: List[str] = []
    parts: Dict[str, float] = {}

    s4 = 12 if d4["trend"] == want else 4 if d4["trend"] == "RANGE" else 0
    s1 = 13 if d1["trend"] == want else 5 if d1["trend"] == "RANGE" else 0
    parts["htf"] = s4 + s1
    aligned = (d4["trend"] == want) + (d1["trend"] == want)
    against = (d4["trend"] == opp) + (d1["trend"] == opp)
    if aligned == 0 or against > 0:
        blockers.append("روند 4H/1H با جهت سناریو هم‌راستا نیست")
    else:
        notes.append("روند 4H: %s | 1H: %s" % (TREND_FA[d4["trend"]], TREND_FA[d1["trend"]]))

    pairs = []
    sw15 = detect_sweep(d15, side, 12)
    sw5 = detect_sweep(d5, side, 24)
    if sw15:
        pairs.append((d15, sw15, 15))
    if sw5:
        pairs.append((d5, sw5, 8))
    chosen = None
    chosen_mss = None
    for d, sw, pts in pairs:
        m = detect_mss(d, side, sw)
        if m:
            chosen, chosen_mss = (d, sw, pts), (m, pts)
            break
    if chosen is None and pairs:
        chosen = pairs[0]
    sweep = chosen[1] if chosen else None
    parts["sweep"] = float(chosen[2]) if chosen else 0.0
    parts["mss"] = float(chosen_mss[1]) if chosen_mss else 0.0
    bos = None
    if sweep:
        pool_txt = " (EQ Pool)" if sweep.get("eq_pool") else ""
        notes.append("شکار نقدینگی در %s%s" % (sweep["tf"], pool_txt))
    if chosen_mss:
        notes.append("MSS قوی تأیید شد (%s)" % chosen_mss[0]["tf"])
    elif aligned == 2:
        bos = detect_bos(d15, side, 6)
        if bos:
            parts["mss"] = 8.0
            notes.append("BOS ادامه‌دهنده در 15m")

    structure_ok = bool(sweep and chosen_mss) or bool(bos)
    if not structure_ok:
        soft.append("ستاپ ساختاری (Sweep+MSS یا BOS) تأیید نشده")

    fib = fib_info(D, side, price)
    ind = indicator_info(D, side)
    if INDICATOR_FILTER and not ind["ok"]:
        blockers.append("فیلتر اندیکاتور ناموفق")

    zone = pick_zone([d15, d5], side, price, atr15)
    entry = price
    if zone:
        inside = zone["bottom"] <= price <= zone["top"]
        mid = (zone["bottom"] + zone["top"]) / 2.0
        entry = price if inside else mid
        entry = min(entry, price) if long else max(entry, price)
        parts["zone"] = 15.0 if zone["kind"] == "OB+FVG" else 10.0 if zone["kind"] == "OB" else 9.0
    else:
        parts["zone"] = 3.0 if structure_ok else 0.0

    refs: List[float] = []
    if zone:
        refs.append(zone["bottom"] if long else zone["top"])
    if sweep:
        refs.append(sweep["extreme"])
    ref = (
        (min(refs) if long else max(refs))
        if refs
        else (entry - 1.5 * atr15 if long else entry + 1.5 * atr15)
    )
    buf = 0.25 * atr15
    sl = ref - buf if long else ref + buf
    risk = abs(entry - sl)
    min_risk = max(MIN_STOP_ATR * atr15, MIN_STOP_PCT / 100.0 * entry, 0.2 * d1["atr"])
    if risk < min_risk:
        risk = min_risk
        sl = entry - risk if long else entry + risk

    draft = {"entry": entry, "sl": sl, "stop_pct": risk / entry * 100.0}
    trade: Optional[Dict[str, Any]] = None
    if risk <= MAX_STOP_ATR * atr15 and risk / entry * 100.0 <= MAX_STOP_PCT:
        levels = collect_levels(D)
        if FIB_CONFIRM and fib["valid"]:
            levels = levels + [p for _, p in fib["targets"]]
        targets = pick_targets(levels, entry, risk, long, FEE_PCT)
        if targets:
            tp1, rr1n = targets[0]
            tp2, rr2n = targets[1] if len(targets) > 1 else (None, None)
            trade = {
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "rr1n": rr1n,
                "rr2n": rr2n,
                "stop_pct": risk / entry * 100.0,
            }

    parts["momentum"] = 10.0 if structure_ok else 4.0
    parts["fib"] = float(fib["pts"]) if FIB_CONFIRM else 0.0
    parts["rr"] = 8.0 if (trade and (trade.get("rr2n") or 0.0) >= 2.0) else 4.0
    parts["room"] = 4.0

    score = int(round(sum(parts.values())))
    if not structure_ok:
        score = min(score, STRUCT_CAP)
    return {
        "side": side,
        "score": score,
        "parts": parts,
        "notes": notes,
        "blockers": blockers,
        "soft_blockers": soft,
        "trade": trade,
        "zone": zone,
        "sweep": sweep,
        "fib": fib,
        "ind": ind,
        "draft": draft,
    }


def fmt_price(p: float) -> str:
    if p >= 1000:
        return "%.2f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p


def local_now() -> str:
    off = _env_float("TZ_OFFSET_HOURS", 3.5)
    t = datetime.now(timezone.utc) + timedelta(hours=off)
    sign = "+" if off >= 0 else "-"
    hh, mm = int(abs(off)), int(round((abs(off) - int(abs(off))) * 60))
    return t.strftime("%Y-%m-%d %H:%M") + " (UTC%s%02d:%02d)" % (sign, hh, mm)


def full_entry_block_fa(r: Dict[str, Any]) -> Optional[List[str]]:
    p = r.get("fib_plan")
    if not p or "منقضی" in p["status"]:
        return None
    side = p["side"]
    price = r["price"]
    entry, sl = p["entry"], p["sl"]
    risk_pct = p["risk_pct"]
    cost = 2.0 * FEE_PCT

    lev = max(1.0, min(MAX_LEVERAGE, TARGET_LEVERAGE))
    notional = CAPITAL_USDT * lev
    actual_risk_usdt = notional * ((risk_pct + cost) / 100.0)
    actual_risk_pct = (actual_risk_usdt / CAPITAL_USDT) * 100.0 if CAPITAL_USDT > 0 else 0.0

    how = "بازار" if abs(entry - price) / price * 100.0 <= 0.1 else "لیمیت"
    badge = "🟢 Sweep+MSS" if p.get("confluence") else ("🟡 Sweep" if p.get("sweep") else "🔵 MSS")

    lines = [
        "<b>%s</b> | <b>%s</b> (%s) | %s" % (html.escape(r["symbol"]), side, how, badge),
        "• ورود: <code>%s</code> (%+.2f%%) | منبع: %s" % (
            fmt_price(entry), p["dist_pct"], html.escape(p.get("entry_src", "فیبو"))
        ),
        "• حد ضرر (SL): <code>%s</code> (%.2f%%)" % (fmt_price(sl), risk_pct),
    ]
    for idx, (tp_key, lbl_key, rr_key) in enumerate(
        [("tp1", "tp1_lbl", "rr1"), ("tp2", "tp2_lbl", "rr2"), ("tp3", "tp3_lbl", "rr3")],
        start=1,
    ):
        val = p.get(tp_key)
        ratio = p.get(rr_key) or 0.0
        lbl = p.get(lbl_key) or ""
        if val is not None and ratio >= MIN_RR1_NET:
            lbl_txt = " [%s]" % html.escape(lbl) if lbl else ""
            lines.append("• TP%d%s: <code>%s</code> (RR خالص %.2f)" % (idx, lbl_txt, fmt_price(val), ratio))
    lines.append(
        "• اهرم: <b>%.0fx</b> | حجم پوزیشن: <b>%.2f</b> | ریسک واقعی: %.2f%% (%.2f واحد)"
        % (lev, notional, actual_risk_pct, actual_risk_usdt)
    )
    lines.append("• وضعیت: %s" % html.escape(p["status"]))
    return lines


def fib_read_fa(D: Dict[str, Dict[str, Any]], price: float, side: Optional[str]) -> str:
    imp = recent_impulse(D["1h"]) or recent_impulse(D["4h"])
    if imp is None:
        return "فیبو: ایمپالس واضح نیست"
    _, B, d = imp["A"], imp["B"], imp["diff"]
    if imp["dir"] == "UP":
        ret = (B - price) / d
        arrow, imp_side = "↑", "LONG"
    else:
        ret = (price - B) / d
        arrow, imp_side = "↓", "SHORT"
    if 0.618 <= ret <= 0.786:
        where = "ناحیه‌ی طلایی ۰٫۶۱۸–۰٫۷۸۶"
    elif 0.5 <= ret < 0.618:
        where = "بین ۰٫۵ و ۰٫۶۱۸"
    elif ret > 0.786:
        where = "پولبک عمیق (>۰٫۷۸۶)"
    elif ret < 0:
        where = "خارج از ایمپالس"
    else:
        where = "پولبک کم (<۰٫۵)"
    align = "هم‌راستا" if side == imp_side else ("مخالف" if side else "")
    return "فیبو: %s (%s %s)" % (where, arrow, align)


def coin_view_fa(r: Dict[str, Any]) -> str:
    D = r["D"]
    price = r["price"]
    score = 0
    for tf, w in (("4h", 2), ("1h", 2), ("15m", 1)):
        t = D[tf]["trend"]
        score += w if t == "UP" else -w if t == "DOWN" else 0
    pd_ = premium_discount(D, price)
    if pd_:
        score += 1 if pd_["pos"] < 50 else -1 if pd_["pos"] > 50 else 0
    if score > 0:
        bias = "لانگ"
    elif score < 0:
        bias = "شورت"
    else:
        bias = "خنثی"
    strength = "قوی" if abs(score) >= 3 else "ضعیف" if abs(score) >= 1 else "خنثی"
    side = "LONG" if bias == "لانگ" else "SHORT" if bias == "شورت" else None
    sw_obj = (
        (detect_sweep(D["15m"], side, 24) or detect_sweep(D["5m"], side, 24))
        if side
        else None
    )
    sweep_txt = "بله (EQ)" if (sw_obj and sw_obj.get("eq_pool")) else ("بله" if sw_obj else "خیر")
    zone = "دیسکانت" if pd_ and pd_["pos"] < 50 else "پریمیوم" if pd_ else "نامشخص"
    trend = "%s%s%s" % (
        TREND_ARROW[D["4h"]["trend"]],
        TREND_ARROW[D["1h"]["trend"]],
        TREND_ARROW[D["15m"]["trend"]],
    )
    plan = r.get("fib_plan")
    if plan and side and plan["side"] == side:
        entry_txt = "ورود %s | SL %s | TP1 %s" % (
            fmt_price(plan["entry"]), fmt_price(plan["sl"]), fmt_price(plan["tp1"])
        )
    elif plan:
        entry_txt = "پلن فعال %s در %s" % (plan["side"], fmt_price(plan["entry"]))
    else:
        entry_txt = "بدون ستاپ تاییدشده"
    fib_txt = fib_read_fa(D, price, side)
    return (
        "<b>%s</b>: <b>%s</b> (%s) | روند: %s | %s | Sweep: %s | %s | %s"
        % (
            html.escape(r["symbol"]),
            bias,
            strength,
            trend,
            zone,
            sweep_txt,
            html.escape(fib_txt),
            html.escape(entry_txt),
        )
    )


def format_report(
    results: List[Dict[str, Any]], errors: List[Tuple[str, str]], sent_signals: int = 0
) -> str:
    lev = max(1.0, min(MAX_LEVERAGE, TARGET_LEVERAGE))
    lines = [
        "📊 <b>گزارش اسکن شکار نقدینگی و فیبوناچی</b> — %s" % local_now(),
        "تنظیم اهرم: <b>%.0fx</b> (حداکثر %.0fx)" % (lev, MAX_LEVERAGE),
    ]
    setups: List[Tuple[int, List[str]]] = []
    for r in results:
        p = r.get("fib_plan")
        block = full_entry_block_fa(r)
        if p and block:
            rank_val = (2 if p.get("confluence") else 1 if p.get("sweep") else 0) * 100 + int(
                round(p.get("rr1", 0.0) * 10)
            )
            setups.append((rank_val, block))
    setups.sort(key=lambda x: -x[0])

    lines.append("ارزهای بررسی‌شده: %d | ستاپ‌های هم‌راستا با چارت: %d" % (len(results), len(setups)))
    if setups:
        lines.append("")
        lines.append("🎯 <b>نقاط ورود، حد ضرر و تارگت‌های مشخص</b>")
        for _, block in setups[:6]:
            lines.append("")
            lines.extend(block)
    else:
        lines.append("")
        lines.append("⏸ در این لحظه ستاپ هم‌راستا با چارت و RR خالص ≥ %.1f پیدا نشد." % MIN_RR1_NET)

    lines.append("")
    lines.append("🧭 <b>دیدگاه تحلیلی هر ارز</b>")
    for r in results:
        try:
            lines.append(coin_view_fa(r))
        except Exception:
            lines.append("%s: تحلیل ناکامل" % html.escape(r["symbol"]))

    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:120])))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Execution, storage & Telegram
# --------------------------------------------------------------------------
def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    try:
        C["1m"] = fetch_klines(symbol, "1m", size=120)
    except ScanError:
        C["1m"] = None
    price = fetch_price(symbol) or C["5m"].c[-1]
    return analyze_candles(symbol, C, price, min_score)


def analyze_candles(
    symbol: str, C: Dict[str, Candles], price: float, min_score: float
) -> Dict[str, Any]:
    D: Dict[str, Dict[str, Any]] = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    last_close = D["5m"]["close"]
    if abs(price - last_close) / last_close > 0.02:
        price = last_close
    regime = classify_regime(D, price)
    gb = global_blockers(D, price, regime)
    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    age_min = (time.time() - (D["5m"]["cs"].t[-1] + TF_MAP["5m"][1])) / 60.0
    res: Dict[str, Any] = {
        "symbol": symbol,
        "price": price,
        "D": D,
        "scenarios": scenarios,
        "global_blockers": gb,
        "signal": None,
        "watch": None,
        "data_age_min": age_min,
        "regime": regime,
        "liq": liquidity_map(D["15m"], price),
    }
    res["fib_plan"] = None if gb else stable_plan(symbol, fib_point_plan(D, price), price)
    return res


def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def write_report_file(parts: List[str]) -> None:
    path = os.getenv("REPORT_FILE", "report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(parts).strip() + "\n")
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


def split_message(text: str, limit: int = 3800) -> List[str]:
    chunks: List[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print(text)
        return False
    ok = True
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    for chunk in split_message(text):
        try:
            resp = http_json(
                url,
                body={
                    "chat_id": chat,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if not isinstance(resp, dict) or not resp.get("ok"):
                ok = False
        except ScanError:
            ok = False
    return ok


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Liquidity Hunt + Fibonacci futures scanner")
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        return 2

    results: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    pairs = fetch_pairs()
    if pairs:
        for sym in [x for x in symbols if x.lower() not in pairs]:
            errors.append((sym, "جفت‌ارز در اسپات LBank پیدا نشد"))
        symbols = [x for x in symbols if x.lower() in pairs]

    for sym in symbols:
        try:
            results.append(analyze_symbol(sym, args.min_score))
        except ScanError as e:
            errors.append((sym, str(e)))
        except Exception as e:
            log.exception("%s: unexpected error", sym)
            errors.append((sym, "unexpected error: %s" % e))

    outbox: List[str] = []
    base_send = (lambda t: print(t) or True) if args.dry_run else send_telegram

    def send(t: str) -> bool:
        outbox.append(to_plain(t))
        return base_send(t)

    if not results:
        send(
            "❗ <b>اسکن ناموفق بود</b>\n"
            + "\n".join("%s: %s" % (html.escape(s), html.escape(e[:150])) for s, e in errors)
        )
        write_report_file(outbox)
        return 1

    rep = format_report(results, errors)
    send(rep)
    write_report_file(outbox)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
