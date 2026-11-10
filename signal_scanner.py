#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Analysis-only crypto futures signal scanner (NO order execution).

Pipeline per symbol:
  1. Fetch price + 5m / 15m / 1H / 4H candles from LBank (public API).
  2. Higher-timeframe STRUCTURE trend (HH/HL, LH/LL, CHoCH); ATR and volume
     are used only as market filters. Price action is the primary method.
  3. Swings, support/resistance, liquidity pools, liquidity sweep, MSS/BOS,
     FVG and Order Blocks.
  4. Score LONG and SHORT scenarios out of 100.
  5. Build Entry / SL / TP1 / TP2 / RR; signal only if score >= MIN_SCORE and
     all risk gates pass (5m structure and momentum included).
  6. Send main report (signals or NO TRADE, with entry/SL/TP and coin read) to Telegram.

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
  FEE_PCT            taker fee per side in %, default 0.06 (set your real fee)
  TP_MIN_RR_NET      each structural target must have net RR >= this, default 0.5
  EXPIRY_H           hours a conditional opportunity stays valid, default 4
  TZ_OFFSET_HOURS    report time zone offset from UTC, default 3.5 (Tehran)
  REPORT_FILE        main Telegram output (plain text), default report.txt
  DETAILS_FILE       full analysis for local review (never sent), default details_report.txt
  SCAN_LOG_FILE      CSV log of every scan (one row per symbol), default scan_log.csv
  DIAG_FILE          per-gate diagnostics (text), default diag_report.txt
  INDICATOR_FILTER   1 = indicator check can block signals; 0 (default) = diagnostics only
  FIB_CONFIRM        1 = Fibonacci affects score/TP; 0 (default) = diagnostics only
  SWING_MIN_AMP      0 (default, off) or a swing needs a move >= this x ATR
  CARD_MIN_RR        display only: hide conditional cards whose TP1 net RR is below this (default 0 = all)

Usage:
  python signal_scanner.py
  python signal_scanner.py --symbols BTC_USDT,ETH_USDT --dry-run
  python signal_scanner.py --diag
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
                   "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,TON_USDT,"
                   "DOT_USDT,LTC_USDT,ATOM_USDT,NEAR_USDT,BCH_USDT")


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
MIN_RR2_NET = 1.5           # min reward/risk to TP2 after round-trip fees (signal gate)
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
            out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            a = sum(trs[:n]) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


def volume_ratio(v: List[float]) -> float:
    """Mean of last 3 closed candles vs mean of the 20 before them."""
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
def find_swings(h: List[float], l: List[float], n: int = 3
                ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and \
           all(h[i] >= h[i + k] for k in range(1, n + 1)):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and \
           all(l[i] <= l[i + k] for k in range(1, n + 1)):
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


def structure_trend(c: List[float], highs: List[Tuple[int, float]],
                    lows: List[Tuple[int, float]]) -> str:
    """Pure price-action trend: HH/HL = UP, LH/LL = DOWN; a close beyond the last
    opposite swing (CHoCH) turns the trend into RANGE."""
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


def filter_swings_amp(h: List[float], l: List[float], highs, lows, at: List[float], min_amp: float):
    """Keep only swings followed by a real move (>= min_amp x ATR) within 10 candles.
    min_amp <= 0 disables the filter (default)."""
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

    return ([p for p in highs if amp_ok(p[0], p[1], True)],
            [p for p in lows if amp_ok(p[0], p[1], False)])


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
        "name": name, "cs": cs, "close": c[-1],
        "atr": max(at[-1], c[-1] * 1e-6), "vol_ratio": volume_ratio(cs.v),
        "highs": highs, "lows": lows, "struct": st, "trend": trend,
        "atr_rank": atr_rank, "atr_list": at,
    }


# --------------------------------------------------------------------------
# Smart-money concepts
# --------------------------------------------------------------------------
def detect_sweep(d: Dict[str, Any], side: str, lookback: int) -> Optional[Dict[str, Any]]:
    """LONG -> sell-side liquidity sweep (wick below swing low, close back above).
    SHORT -> buy-side sweep (wick above swing high, close back below)."""
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
                ok = cs.l[j] < lvl - 0.03 * a and cs.c[j] > lvl and \
                    all(x >= lvl for x in cs.c[si + 1:j])
                ext = min(cs.l[j:]) if ok else 0.0
            else:
                ok = cs.h[j] > lvl + 0.03 * a and cs.c[j] < lvl and \
                    all(x <= lvl for x in cs.c[si + 1:j])
                ext = max(cs.h[j:]) if ok else 0.0
            if ok:
                return {"idx": j, "level": lvl, "extreme": ext, "age": n - 1 - j, "tf": d["name"]}
    return None


def detect_mss(d: Dict[str, Any], side: str, sweep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """MSS: strong-bodied close beyond the last opposite swing, shortly after the sweep."""
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
    """Continuation break of structure in the last `within` candles."""
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


def find_fvgs(cs: Candles, atr_v: float, side: str, lookback: int = 50) -> List[Dict[str, float]]:
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


def find_obs(cs: Candles, atr_v: float, side: str, lookback: int = 60) -> List[Dict[str, float]]:
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


def pick_zone(ds: List[Dict[str, Any]], side: str, price: float, atr15: float
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
PART_MAX = {"htf": 25, "sweep": 15, "mss": 15, "zone": 15,
            "momentum": 10 if FIB_CONFIRM else 15, "rr": 10, "room": 5,
            "fib": FIB_PTS_OVERLAP if FIB_CONFIRM else 0}
PART_HINT = {
    "htf": "هم‌راستاشدن روند 4H/1H",
    "sweep": "Sweep نقدینگی",
    "mss": "MSS قوی",
    "zone": "برگشت قیمت به ناحیه OB/FVG",
    "momentum": "کندل‌های جابه‌جایی هم‌جهت و ساختار 5m",
    "rr": "RR خالص بهتر",
    "room": "فضای بیشتر تا مانع HTF",
    "fib": "تأیید فیبوناچی (ناحیه پولبک + OB/FVG)",
}


def make_watch(res: Dict[str, Any], sc: Dict[str, Any], extra: str = "") -> Dict[str, Any]:
    liq = res["liq"]
    if sc["soft_blockers"]:
        if sc["side"] == "LONG":
            lvl = liq["ssl"]
            cond = ("جاروب زیر SSL≈%s، بسته‌شدن دوباره بالای آن، سپس MSS قوی در 5m/15m" % fmt_price(lvl)
                    if lvl is not None else "منتظر Sweep نقدینگی فروش + MSS قوی")
        else:
            lvl = liq["bsl"]
            cond = ("جاروب بالای BSL≈%s، بسته‌شدن دوباره زیر آن، سپس MSS قوی در 5m/15m" % fmt_price(lvl)
                    if lvl is not None else "منتظر Sweep نقدینگی خرید + MSS قوی")
    elif sc["score"] >= MIN_SCORE:
        cond = "امتیاز و ساختار برقرار است؛ فقط محدودیت‌های اسکن مانع صدور شده"
    else:
        soon = ("zone", "momentum", "rr", "room")
        gaps = [(PART_MAX[k] - sc["parts"].get(k, 0.0), k) for k in soon]
        gaps = sorted([g for g in gaps if g[0] > 0], reverse=True)[:2]
        txt = ("، ".join("%s (−%d)" % (PART_HINT[k], round(g)) for g, k in gaps)
               if gaps else "Sweep/MSS یا هم‌راستایی بهتر HTF")
        cond = "ساختار تأیید شده؛ امتیاز %d از %d (کمبود %d). برای تکمیل: %s" % (
            sc["score"], MIN_SCORE, MIN_SCORE - sc["score"], txt)
    return {"side": sc["side"], "score": sc["score"], "trade": sc["trade"],
            "condition": cond, "extra": extra}


def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    price = fetch_price(symbol) or C["5m"].c[-1]
    return analyze_candles(symbol, C, price, min_score)


def analyze_candles(symbol: str, C: Dict[str, Candles], price: float, min_score: float) -> Dict[str, Any]:
    """Same decision logic as the live scan, but on any candle set (live or historical)."""
    D: Dict[str, Dict[str, Any]] = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    last_close = D["5m"]["close"]
    if abs(price - last_close) / last_close > 0.02:
        log.warning("%s ticker %.6f deviates from last close %.6f; using close", symbol, price, last_close)
        price = last_close
    regime = classify_regime(D, price)
    gb = global_blockers(D, price, regime)
    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    bo = regime["breakout"]
    for sc in scenarios.values():
        want_dir = "UP" if sc["side"] == "LONG" else "DOWN"
        if bo and bo["dir"] == want_dir:
            sc["notes"].append("شکست معتبر از فشردگی (بدنه‌دار + حجم) بالای/زیر %s" % fmt_price(bo["level"]))
        elif regime["name"] == "QUIET":
            sc["blockers"].insert(0, "بازار فشرده؛ ورود فقط پس از شکست معتبر (بدنه‌دار + حجم)")
    signal = None
    for s in sorted(scenarios.values(), key=lambda x: -x["score"]):
        if (s["score"] >= min_score and not s["blockers"] and not s["soft_blockers"]
                and not gb and s["trade"]):
            signal = s
            break
    age_min = (time.time() - (D["5m"]["cs"].t[-1] + TF_MAP["5m"][1])) / 60.0
    res: Dict[str, Any] = {
        "symbol": symbol, "price": price, "D": D, "scenarios": scenarios,
        "global_blockers": gb, "signal": signal, "watch": None,
        "reason_override": None, "data_age_min": age_min, "regime": regime,
        "liq": liquidity_map(D["15m"], price),
    }
    if not signal and not gb:
        cands = [s for s in scenarios.values()
                 if s["trade"] and not s["blockers"] and s["score"] >= WATCH_MIN]
        if cands:
            res["watch"] = make_watch(res, max(cands, key=lambda x: x["score"]))
    return res


def fmt_price(p: float) -> str:
    if p >= 1000:
        return "%.2f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p


def format_signal(res: Dict[str, Any], sc: Dict[str, Any]) -> str:
    t = sc["trade"]
    long = sc["side"] == "LONG"
    icon = "🟢" if long else "🔴"
    lines = [
        "%s <b>سیگنال قطعی %s — %s</b>" % (icon, sc["side"], html.escape(res["symbol"])),
        "امتیاز: <b>%d/100</b>" % sc["score"],
        "",
        "ورود (%s): <code>%s</code>" % (t["order_type"], fmt_price(t["entry"])),
        "حد ضرر: <code>%s</code> (%.2f%%)" % (fmt_price(t["sl"]), t["stop_pct"]),
        "TP1: <code>%s</code> (RR %.2f | خالص %.2f)" % (fmt_price(t["tp1"]), t["rr1"], t["rr1n"]),
        "TP2: <code>%s</code> (RR %.2f | خالص %.2f)" % (fmt_price(t["tp2"]), t["rr2"], t["rr2n"]),
        "(RR خالص بعد از کارمزد رفت‌وبرگشت %.2f%%)" % t["cost_pct"],
        "",
        "ریسک پیشنهادی: %.2f%% سرمایه (شامل کارمزد) | حداکثر اهرم: %.0fx" % (RISK_PCT, MAX_LEVERAGE),
        "اهرم مؤثر پیشنهادی: %.2fx" % t["notional_x"],
    ]
    if t["capped"]:
        lines.append("⚠️ به‌دلیل سقف اهرم، ریسک مؤثر ≈ %.2f%% است" % t["eff_risk_pct"])
    eq = _env_float("ACCOUNT_EQUITY", 0.0)
    if eq > 0:
        lines.append("حجم پوزیشن (تقریبی): %.2f USDT" % (eq * t["notional_x"]))
    lines.append("")
    lines.append("دلایل:")
    for n in sc["notes"]:
        lines.append("• " + html.escape(n))
    lines.append("")
    lines.append("قیمت فعلی: %s | سن داده: %.0f دقیقه" % (fmt_price(res["price"]), max(res["data_age_min"], 0.0)))
    lines.append("اگر قبل از فعال‌شدن ورود، حد ضرر لمس شود سیگنال باطل است.")
    lines.append("⚠️ فقط تحلیل است؛ سفارشی به‌صورت خودکار ثبت نمی‌شود.")
    return "\n".join(lines)


def no_signal_reason(res: Dict[str, Any]) -> str:
    if res.get("reason_override"):
        return res["reason_override"]
    if res["global_blockers"]:
        return res["global_blockers"][0]
    lg, sh = res["scenarios"]["LONG"], res["scenarios"]["SHORT"]
    best = lg if lg["score"] >= sh["score"] else sh
    if best["blockers"]:
        return best["blockers"][0]
    if best["soft_blockers"]:
        return best["soft_blockers"][0]
    return "امتیاز %d کمتر از حداقل %d است" % (best["score"], MIN_SCORE)


def build_card(res: Dict[str, Any]) -> Dict[str, Any]:
    """Complete trade plan for the dominant side, taken from the scanner's own trade object.
    Targets are structural and net-RR positive only; a second target is shown only if it is
    valid. Incomplete confirmations never remove the plan: they are listed as missing items."""
    ls, ss = res["scenarios"]["LONG"], res["scenarios"]["SHORT"]
    dom = "SHORT" if ss["score"] >= ls["score"] else "LONG"
    short = dom == "SHORT"
    sc = ss if short else ls
    price = res["price"]
    t = sc["trade"]
    missing: List[str] = []
    for m in list(res.get("global_blockers", [])) + list(sc["soft_blockers"]) + list(sc["blockers"]):
        if m not in missing:
            missing.append(m)
    if sc["score"] < MIN_SCORE:
        missing.append("امتیاز %d از %d (کمبود %d)" % (sc["score"], MIN_SCORE, MIN_SCORE - sc["score"]))
    if res.get("signal") and res["signal"]["side"] == dom:
        status = "سیگنال صادر شد"
    elif missing:
        status = "در انتظار: " + missing[0]
    else:
        status = "شرط‌ها کامل؛ منتظر تأیید نهایی"
    if res.get("signal") and res["signal"]["side"] == dom:
        decision = "ENTER"
    elif t is None:
        decision = "NO_ENTRY"
    else:
        decision = "CONDITIONAL"
    if t:
        when = "ورود %s به %s" % ("لیمیت" if t["order_type"] == "LIMIT" else "بازار", fmt_price(t["entry"]))
        retest = (short and t["entry"] > price) or ((not short) and t["entry"] < price)
        if retest:
            activation = ("پولبک به %s و رد سطح با کندل 15m هم‌جهت و حجم ≥ %.1f× میانگین (%s)"
                          % (fmt_price(t["entry"]), BREAKOUT_MIN_VOL, when))
        else:
            side_word = "زیر" if short else "بالای"
            activation = ("تأیید: کندل 15m بسته‌شده %s %s با حجم ≥ %.1f× میانگین (%s)"
                          % (side_word, fmt_price(t["entry"]), BREAKOUT_MIN_VOL, when))
    else:
        activation = "—"
    return {"side": dom, "score_short": ss["score"], "score_long": ls["score"], "status": status,
            "trade": t, "missing": missing, "short": short, "activation": activation,
            "price": price, "best_score": max(ls["score"], ss["score"]), "decision": decision}


def plan_fields(res: Dict[str, Any]) -> Dict[str, Any]:
    """Standard scenario fields for display: Activation Price, Confirmation, Entry Zone,
    Invalidation, SL, TP1/TP2, Net RR, Leverage, Expiry. All numbers come from the trade."""
    c = build_card(res)
    t = c["trade"]
    short = c["short"]
    atr15 = res["D"]["15m"]["atr"]
    off = _env_float("TZ_OFFSET_HOURS", 3.5)
    exp_t = datetime.now(timezone.utc) + timedelta(hours=off + EXPIRY_H)
    expiry = exp_t.strftime("%Y-%m-%d %H:%M") + " (محلی)"
    f = {"side": c["side"], "decision": DECISION_FA[c["decision"]], "expiry": expiry,
         "missing": c["missing"], "status": c["status"], "trade": t}
    if t is None:
        f.update({"activation": None, "confirmation": "—", "zone": None,
                  "invalidation": "—", "sl": None, "tp1": None, "tp2": None,
                  "rr1": None, "rr2": None, "leverage": None})
        return f
    zone = None
    sc = res["scenarios"]["SHORT" if short else "LONG"]
    if sc.get("zone"):
        zone = (sc["zone"]["bottom"], sc["zone"]["top"])
    if zone is None:
        zone = (t["entry"] - 0.1 * atr15, t["entry"] + 0.1 * atr15)
    side_word = "زیر" if short else "بالای"
    f.update({
        "activation": t["entry"],
        "confirmation": "کندل 15m بسته‌شده %s %s با حجم ≥ %.1f× میانگین" % (
            side_word, fmt_price(t["entry"]), BREAKOUT_MIN_VOL),
        "zone": zone,
        "invalidation": "کندل 15m بسته‌شده %s %s (حد ضرر)" % (
            "بالای" if short else "زیر", fmt_price(t["sl"])),
        "sl": t["sl"], "tp1": t["tp1"], "tp2": t["tp2"] if t.get("tp2_ok") else None,
        "rr1": t["rr1n"], "rr2": t["rr2n"] if t.get("tp2_ok") else None,
        "leverage": t["notional_x"],
    })
    return f


def format_card(res: Dict[str, Any]) -> List[str]:
    """Standard field card (details / per-symbol view)."""
    p = plan_fields(res)
    f = fmt_price
    lines = [
        "<b>%s</b> | %s | %s | انقضا: %s" % (html.escape(res["symbol"]), p["side"], p["decision"],
                                           html.escape(p["expiry"])),
    ]
    if p["trade"] is None:
        lines.append("طرح معامله: — (تارگت یا حد ضرر معتبر پیدا نشد)")
    else:
        lines.append("Activation Price: %s" % f(p["activation"]))
        lines.append("Confirmation: %s" % html.escape(p["confirmation"]))
        lines.append("Entry Zone: %s – %s" % (f(p["zone"][0]), f(p["zone"][1])))
        lines.append("Invalidation: %s" % html.escape(p["invalidation"]))
        lines.append("SL: %s (%.2f%%)" % (f(p["sl"]), p["trade"]["stop_pct"]))
        tp2 = ("%s (Net RR %.2f)" % (f(p["tp2"]), p["rr2"])) if p["tp2"] is not None else "— (معتبر پیدا نشد)"
        lines.append("TP1: %s (Net RR %.2f) | TP2: %s" % (f(p["tp1"]), p["rr1"], tp2))
        lines.append("Leverage: ≤ %.2fx (ریسک %.2f%% ÷ فاصله‌ی حد ضرر + کارمزد)" % (p["leverage"], RISK_PCT))
    lines.append("وضعیت: %s" % html.escape(p["status"]))
    rest = p["missing"][1:]
    if rest:
        lines.append("نواقص دیگر: " + html.escape(" | ".join(rest)))
    return lines


def local_now() -> str:
    """Report timestamp in the viewer's local time (TZ_OFFSET_HOURS, default +3.5 for Tehran)."""
    off = _env_float("TZ_OFFSET_HOURS", 3.5)
    t = datetime.now(timezone.utc) + timedelta(hours=off)
    sign = "+" if off >= 0 else "-"
    hh, mm = int(abs(off)), int(round((abs(off) - int(abs(off))) * 60))
    return t.strftime("%Y-%m-%d %H:%M") + " (UTC%s%02d:%02d)" % (sign, hh, mm)


def format_brief(res: Dict[str, Any]) -> List[str]:
    """Compact Telegram card with the standard fields."""
    p = plan_fields(res)
    f = fmt_price
    D = res["D"]
    side_key = "LONG" if p["side"] == "LONG" else "SHORT"
    sc = res["scenarios"][side_key]
    read = [n for n in sc["notes"] if not n.startswith("حجم") and not n.startswith("مومنتوم")][:3]
    lines = [
        "<b>%s</b> | %s | %s | انقضا: %s" % (html.escape(res["symbol"]), p["side"],
                                          p["decision"], html.escape(p["expiry"])),
        "روند 4H/1H/15m: %s %s %s" % (TREND_ARROW[D["4h"]["trend"]], TREND_ARROW[D["1h"]["trend"]],
                                     TREND_ARROW[D["15m"]["trend"]]),
    ]
    if read:
        lines.append("تحلیل: " + html.escape(" | ".join(read)))
    if p["trade"] is None:
        lines.append("طرح معامله: — (تارگت معتبر نیست؛ ورود نکن)")
    else:
        lines.append("Activation: %s | Entry Zone: %s – %s" % (
            f(p["activation"]), f(p["zone"][0]), f(p["zone"][1])))
        lines.append("Confirmation: %s" % html.escape(p["confirmation"]))
        lines.append("Invalidation: %s" % html.escape(p["invalidation"]))
        lines.append("SL: %s (%.2f%%) | TP1: %s | TP2: %s" % (
            f(p["sl"]), p["trade"]["stop_pct"], f(p["tp1"]),
            f(p["tp2"]) if p["tp2"] is not None else "—"))
        lines.append("Net RR: TP1 %.2f%s | Leverage ≤ %.2fx" % (
            p["rr1"], (" / TP2 %.2f" % p["rr2"]) if p["rr2"] is not None else "", p["leverage"]))
    fp = fib_plan(D, side_key, res["price"])
    if fp:
        lines.append("پلن فیبو: %s" % html.escape(fp["status"]))
    if p["decision"] != DECISION_FA["ENTER"] and p["missing"]:
        lines.append("نواقص: " + html.escape(" | ".join(p["missing"][:2])))
    return lines
def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]],
                  sent_signals: int) -> str:
    """Main Telegram output: signals or NO TRADE, plus compact entry/SL/TP and coin read
    for the strongest symbols. Full analysis goes to details_report.txt."""
    now = local_now()
    sigs = [r for r in results if r["signal"]]
    lines = ["📊 <b>گزارش اسکن</b> — %s" % now]
    if sigs:
        lines.append("🎯 سیگنال قطعی: %d (جزئیات در پیام جداگانه)" % len(sigs))
    else:
        lines.append("NO TRADE — هیچ سیگنال قطعی صادر نشد (%d ارز بررسی شد)." % len(results))
    top = sorted(results, key=lambda r: -max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]))
    shown = [r for r in top if max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]) >= WATCH_MIN
             and not r["signal"]][:5]
    if shown:
        lines.append("")
        lines.append("🧭 <b>طرح‌ها و تحلیل (امتیاز ≥ %d)</b>" % WATCH_MIN)
        for r in shown:
            lines.append("")
            lines.extend(format_brief(r))
    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:150])))
    lines.append("")
    lines.append("حداقل امتیاز: %d | ترتیب روند: 4H/1H/15m" % MIN_SCORE)
    return "\n".join(lines)


def format_details(results: List[Dict[str, Any]], errors: List[Tuple[str, str]],
                  sent_signals: int) -> str:
    now = local_now()
    lines = ["📊 <b>گزارش اسکن</b> — %s" % now, ""]
    sigs = [r for r in results if r["signal"]]
    lines.append("🎯 سیگنال قطعی: %d" % len(sigs))
    for r in sigs:
        lines.append("• %s %s (امتیاز %d)" % (html.escape(r["symbol"]), r["signal"]["side"], r["signal"]["score"]))
    watches = [r for r in results if r.get("watch") and not r["signal"]]
    if watches:
        lines.append("")
        lines.append("👀 <b>Watchlist</b> — هنوز سیگنال نیست؛ ورود فقط بعد از تحقق شرط:")
        for r in watches:
            w = r["watch"]
            t = w["trade"]
            lines.append("")
            lines.append("<b>%s %s</b> | امتیاز %d | قیمت %s" % (
                html.escape(r["symbol"]), w["side"], w["score"], fmt_price(r["price"])))
            lines.append("شرط فعال‌سازی: " + html.escape(w["condition"]))
            lines.append("سناریو: ورود %s | SL %s | TP1 %s | TP2 %s | RR خالص %.2f" % (
                fmt_price(t["entry"]), fmt_price(t["sl"]), fmt_price(t["tp1"]),
                (fmt_price(t["tp2"]) if t.get("tp2_ok", True) else "—"), t["rr2n"]))
            if w["extra"]:
                lines.append("توضیح: " + html.escape(w["extra"]))
    breakouts = [r for r in results if r["regime"]["breakout"]]
    if breakouts:
        lines.append("")
        lines.append("🚀 <b>شکست معتبر از فشردگی</b>")
        for r in breakouts:
            b = r["regime"]["breakout"]
            lines.append("%s | %s | سطح %s (رنج %s تا %s)" % (
                html.escape(r["symbol"]), "صعودی" if b["dir"] == "UP" else "نزولی",
                fmt_price(b["level"]), fmt_price(b["range_lo"]), fmt_price(b["range_hi"])))
    squeezes = [r for r in results if r["regime"]["name"] == "QUIET" and not r["regime"]["breakout"]]
    if squeezes:
        lines.append("")
        lines.append("⏳ <b>فشردگی نوسان</b> — منتظر شکست معتبر:")
        for r in squeezes:
            q = r["regime"]["squeeze"]
            lines.append("%s | رنج ۲۰ کندل 15m: بالا %s | پایین %s (≈ %.1f ATR، صدک %.0f%%)" % (
                html.escape(r["symbol"]), fmt_price(q["hi"]), fmt_price(q["lo"]),
                q["range_atr"], q["range_rank"] * 100.0))
        lines.append("شکست معتبر = کندل 15m با بدنه ≥ 0.8 ATR بیرون از رنج و بسته‌شدن بیرون آن، با حجم ≥ 1.2× میانگین رنج.")

    def _card_ok(r: Dict[str, Any]) -> bool:
        c = build_card(r)
        if c["trade"] is None or CARD_MIN_RR <= 0:
            return True
        return c["trade"]["rr1n"] >= CARD_MIN_RR

    cards = sorted([r for r in results
                    if max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]) >= WATCH_MIN
                    and _card_ok(r)],
                   key=lambda r: -max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]))
    lines.append("")
    lines.append("🧭 <b>فرصت‌های شرطی (امتیاز ≥ %d)</b>" % WATCH_MIN)
    lines.append("این‌ها سیگنال نیستند؛ اعداد از داده‌ی بازار محاسبه شده‌اند و پیش‌بینی نیستند.")
    if not cards:
        lines.append("در این اسکن فرصت شرطی وجود ندارد.")
    for r in cards[:10]:
        lines.append("")
        lines.extend(format_card(r))
    lines.append("")
    lines.append("🔎 <b>خلاصه‌ی بازار</b>")
    for r in results:
        D = r["D"]
        lg, sh = r["scenarios"]["LONG"], r["scenarios"]["SHORT"]
        if r["signal"]:
            status = "✅ سیگنال %s" % r["signal"]["side"]
        elif r.get("watch"):
            status = "👀 Watchlist"
        else:
            status = "⛔ " + html.escape(no_signal_reason(r))
        age = max(r["data_age_min"], 0.0)
        stale = " ⚠️ داده قدیمی" if age > 20 else ""
        liq = r["liq"]
        pool = []
        if liq["bsl"] is not None:
            pool.append("BSL%s %s" % ("(EQH)" if liq["bsl_eq"] else "", fmt_price(liq["bsl"])))
        if liq["ssl"] is not None:
            pool.append("SSL%s %s" % ("(EQL)" if liq["ssl_eq"] else "", fmt_price(liq["ssl"])))
        lines.append("<b>%s</b> %s | %s%s%s | L%d S%d | نوسان %s | %s | داده %.0fد" % (
            html.escape(r["symbol"]), fmt_price(r["price"]),
            TREND_ARROW[D["4h"]["trend"]], TREND_ARROW[D["1h"]["trend"]], TREND_ARROW[D["15m"]["trend"]],
            lg["score"], sh["score"], REGIME_FA[r["regime"]["name"]], status, age) + stale)
        if pool:
            lines.append("   نقدینگی: " + " | ".join(pool))
    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:150])))
    lines.append("")
    lines.append("ترتیب روند: 4H/1H/15m | سیگنال ارسال‌شده: %d | حداقل امتیاز: %d" % (sent_signals, MIN_SCORE))
    return "\n".join(lines)


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
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead")
        print(text)
        return False
    ok = True
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    for chunk in split_message(text):
        try:
            resp = http_json(url, body={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                        "disable_web_page_preview": True})
            if not isinstance(resp, dict) or not resp.get("ok"):
                log.error("telegram rejected message: %s", str(resp)[:200])
                ok = False
        except ScanError as e:
            log.error("telegram send failed: %s", e)
            ok = False
    return ok


# --------------------------------------------------------------------------
# Cooldown state (best effort)
# --------------------------------------------------------------------------
def load_state() -> Dict[str, float]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_state(state: Dict[str, float]) -> None:
    try:
        cutoff = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cutoff}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        log.warning("could not save state: %s", e)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def write_report_file(parts: List[str]) -> None:
    path = os.getenv("REPORT_FILE", "report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(parts).strip() + "\n")
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


SCAN_LOG_FIELDS = [
    "ts_utc", "symbol", "price", "last_candle_utc", "trend_4h", "trend_1h", "trend_15m",
    "long_score", "short_score", "regime", "status", "side", "entry", "sl", "tp1", "tp2",
    "rr2_net", "stop_pct", "reason", "fib_valid", "fib_overlap",
]


def append_scan_log(results: List[Dict[str, Any]]) -> None:
    """Append one row per symbol so Watchlist/Signal outcomes can be reviewed later."""
    path = os.getenv("SCAN_LOG_FILE", SCAN_LOG_FILE)
    if not path:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        new_file = (not os.path.exists(path)) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SCAN_LOG_FIELDS)
            if new_file:
                w.writeheader()
            for r in results:
                D = r["D"]
                lg, sh = r["scenarios"]["LONG"], r["scenarios"]["SHORT"]
                if r["signal"]:
                    status, sc = "SIGNAL", r["signal"]
                elif r.get("watch"):
                    status, sc = "WATCH", r["scenarios"][r["watch"]["side"]]
                else:
                    status, sc = "NONE", (lg if lg["score"] >= sh["score"] else sh)
                t = sc["trade"] or {}
                last_t = D["5m"]["cs"].t[-1] + TF_MAP["5m"][1]
                row = {
                    "ts_utc": now, "symbol": r["symbol"], "price": r["price"],
                    "last_candle_utc": datetime.fromtimestamp(last_t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "trend_4h": D["4h"]["trend"], "trend_1h": D["1h"]["trend"], "trend_15m": D["15m"]["trend"],
                    "long_score": lg["score"], "short_score": sh["score"],
                    "regime": r["regime"]["name"], "status": status, "side": sc["side"],
                    "entry": t.get("entry", ""), "sl": t.get("sl", ""),
                    "tp1": t.get("tp1", ""), "tp2": t.get("tp2", ""),
                    "rr2_net": round(t["rr2n"], 3) if (t and t.get("rr2n") is not None) else "",
                    "stop_pct": round(t["stop_pct"], 3) if t else "",
                    "reason": "" if r["signal"] else no_signal_reason(r),
                    "fib_valid": sc.get("fib", {}).get("valid", ""),
                    "fib_overlap": sc.get("fib", {}).get("overlap", ""),
                }
                w.writerow(row)
    except OSError as e:
        log.warning("could not write scan log %s: %s", path, e)


def gate_table(res: Dict[str, Any], min_score: float) -> List[str]:
    """One line per gate for each side: PASS / FAIL with the measured value.
    Diagnostic only; does not change any decision."""
    D = res["D"]
    out: List[str] = []
    out.append("=" * 70)
    out.append("%s | price %s | regime %s | min_score %d" % (
        res["symbol"], fmt_price(res["price"]), res["regime"]["name"], min_score))
    out.append("  trend 4H=%s 1H=%s 15m=%s 5m=%s | struct 15m=%d 5m=%d" % (
        D["4h"]["trend"], D["1h"]["trend"], D["15m"]["trend"], D["5m"]["trend"],
        D["15m"]["struct"], D["5m"]["struct"]))
    out.append("  vol_ratio 15m=%.2f | atr_rank 15m=%.2f | atr%% 15m=%.3f%%" % (
        D["15m"]["vol_ratio"], D["15m"]["atr_rank"], res["regime"]["atr_pct"] * 100.0))
    for name in ("LONG", "SHORT"):
        sc = res["scenarios"][name]
        t = sc["trade"]
        out.append("  --- %s score=%d parts=%s" % (name, sc["score"],
                   ", ".join("%s=%d" % (k, round(v)) for k, v in sc["parts"].items())))
        want = "UP" if name == "LONG" else "DOWN"
        aligned_ok = D["4h"]["trend"] == want or D["1h"]["trend"] == want
        against = D["4h"]["trend"] == ("DOWN" if name == "LONG" else "UP") or \
            D["1h"]["trend"] == ("DOWN" if name == "LONG" else "UP")
        out.append("    [HTF aligned]      %s  (4H=%s 1H=%s)" % (
            "PASS" if aligned_ok and not against else "FAIL", D["4h"]["trend"], D["1h"]["trend"]))
        out.append("    [structure]        %s  (sweep=%s mss=%s)" % (
            "PASS" if not sc["soft_blockers"] else "FAIL",
            "yes" if sc["sweep"] else "no", "yes" if sc["parts"].get("mss") else "no"))
        if t:
            out.append("    [stop distance]    %s  (%.3f%%, max %.1f%%)" % (
                "PASS" if not any("حد ضرر" in b for b in sc["blockers"]) else "FAIL",
                t["stop_pct"], MAX_STOP_PCT))
            out.append("    [RR net to TP2]    %s  (%.2f, min %.1f, fee %.2f%%/side)" % (
                "PASS" if (t["rr2n"] or 0.0) >= MIN_RR2_NET else "FAIL", (t["rr2n"] or 0.0), MIN_RR2_NET, FEE_PCT))
            out.append("    [HTF obstacle]     %s  (room in R: %s)" % (
                "FAIL" if any("مانع HTF" in b for b in sc["blockers"]) else "PASS",
                "see blockers"))
        else:
            out.append("    [trade levels]     FAIL  (no trade built)")
        out.append("    [score>=min]       %s  (%d)" % ("PASS" if sc["score"] >= min_score else "FAIL", sc["score"]))
        fb = sc.get("fib", {})
        fz = fb.get("zone")
        out.append("    [FIB] valid=%s retr=%s zone=%s overlap=%s in_zone=%s pts=%d (%s) targets=%s" % (
            fb.get("valid"), ("%.2f" % fb["retr"]) if fb.get("retr") is not None else "-",
            ("%s..%s" % (fmt_price(fz[0]), fmt_price(fz[1]))) if fz else "-",
            fb.get("overlap"), fb.get("in_zone"), fb.get("pts", 0),
            "APPLIED" if FIB_CONFIRM else "info only",
            ", ".join("%.3f->%s" % (k, fmt_price(p)) for k, p in fb.get("targets", [])) or "-"))
        ind = sc.get("ind", {})
        if ind:
            out.append("    [INDICATORS] %d/%d passed (ok=%s, mode=%s) failed=%s rsi=%.1f" % (
                ind["passed"], ind["total"], ind["ok"],
                "FILTER" if INDICATOR_FILTER else "info only",
                ",".join(ind["failed"]) or "-", ind["rsi"]))
        if sc["blockers"]:
            out.append("    blockers: " + " | ".join(sc["blockers"]))
    out.append("  global: " + (" | ".join(res["global_blockers"]) if res["global_blockers"] else "none"))
    out.append("  => decision: %s" % (
        ("SIGNAL " + res["signal"]["side"]) if res["signal"] else
        ("WATCH " + res["watch"]["side"]) if res.get("watch") else
        "NONE (" + no_signal_reason(res) + ")"))
    return out


def write_diag(results: List[Dict[str, Any]], min_score: float) -> None:
    """Write the diagnostic report locally only (never sent to Telegram)."""
    path = os.getenv("DIAG_FILE", DIAG_FILE)
    if not path:
        return
    now = local_now()
    lines = ["DIAGNOSTIC REPORT — %s" % now, ""]
    for r in results:
        lines.extend(gate_table(r, min_score))
        lines.append("")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.warning("could not write diag report %s: %s", path, e)


def write_details(results: List[Dict[str, Any]], errors: List[Tuple[str, str]], sent: int) -> None:
    """Full analysis (watchlist, conditional plans, market summary) for local review only."""
    path = os.getenv("DETAILS_FILE", "details_report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_plain(format_details(results, errors, sent)) + "\n")
    except OSError as e:
        log.warning("could not write details %s: %s", path, e)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Crypto futures signal scanner (analysis only)")
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true", help="print only, do not send to Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--diag", action="store_true", help="also write per-gate diagnostic report")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        log.error("no symbols given")
        return 2

    results: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    pairs = fetch_pairs()
    if pairs:
        for sym in [x for x in symbols if x.lower() not in pairs]:
            errors.append((sym, "جفت‌ارز در اسپات LBank پیدا نشد؛ نادیده گرفته شد"))
        symbols = [x for x in symbols if x.lower() in pairs]
    for sym in symbols:
        try:
            results.append(analyze_symbol(sym, args.min_score))
            log.info("%s scanned", sym)
        except ScanError as e:
            log.error("%s: %s", sym, e)
            errors.append((sym, str(e)))
        except Exception as e:  # keep the scan alive on unexpected per-symbol bugs
            log.exception("%s: unexpected error", sym)
            errors.append((sym, "unexpected error: %s" % e))

    outbox: List[str] = []
    base_send = (lambda t: print(t) or True) if args.dry_run else send_telegram

    def send(t: str) -> bool:
        outbox.append(to_plain(t))
        return base_send(t)

    if not results:
        send("❗ <b>اسکن ناموفق بود</b>\n" + "\n".join(
            "%s: %s" % (html.escape(s), html.escape(e[:150])) for s, e in errors))
        write_report_file(outbox)
        return 1

    cnt = {"LONG": 0, "SHORT": 0}
    for r in sorted([x for x in results if x["signal"]], key=lambda x: -x["signal"]["score"]):
        sd = r["signal"]["side"]
        if cnt[sd] >= MAX_SAME_DIR:
            r["watch"] = make_watch(r, r["signal"], "سقف سیگنال هم‌جهت در یک اسکن")
            r["reason_override"] = "سقف سیگنال هم‌جهت در یک اسکن"
            r["signal"] = None
        else:
            cnt[sd] += 1

    state = load_state()
    sent = 0
    for res in results:
        sig = res["signal"]
        if not sig:
            continue
        key = "%s:%s" % (res["symbol"], sig["side"])
        if time.time() - state.get(key, 0.0) < COOLDOWN_H * 3600:
            log.info("%s in cooldown, skipped", key)
            res["signal"] = None
            res["reason_override"] = "سیگنال مشابه اخیراً ارسال شده (cooldown)"
            continue
        if send(format_signal(res, sig)) or args.dry_run:
            state[key] = time.time()
            sent += 1
    if not args.dry_run:
        save_state(state)
        append_scan_log(results)
    if args.diag or args.dry_run:
        write_diag(results, args.min_score)
    write_details(results, errors, sent)
    send(format_report(results, errors, sent))
    write_report_file(outbox)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)