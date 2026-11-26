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
CONDITIONAL_MIN_RR = _env_float("CONDITIONAL_MIN_RR", 1.0)  # TP1 net RR needed to show a conditional plan as waiting
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


def filter_swings_amp(
    h: List[float], l: List[float], highs, lows, at: List[float], min_amp: float
):
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
                return {"idx": j, "level": lvl, "extreme": ext, "age": n - 1 - j, "tf": d["name"]}
    return None


def detect_mss(
    d: Dict[str, Any], side: str, sweep: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
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
# Scenario evaluation
# --------------------------------------------------------------------------
def squeeze_info(d: Dict[str, Any], n: int = BREAKOUT_WINDOW) -> Dict[str, float]:
    """Current n-candle range, in ATR and as a percentile of this symbol's own
    recent n-candle ranges (adaptive: no fixed threshold shared by all coins)."""
    cs: Candles = d["cs"]
    total = len(cs)
    hi, lo = max(cs.h[-n:]), min(cs.l[-n:])
    cur = hi - lo
    start = max(n, total - 200)
    ranges = [max(cs.h[i - n:i]) - min(cs.l[i - n:i]) for i in range(start, total + 1)]
    rank = sum(1 for x in ranges if x <= cur) / float(len(ranges))
    return {"hi": hi, "lo": lo, "range_atr": cur / d["atr"], "range_rank": rank}


def detect_breakout(d: Dict[str, Any], within: int = 3) -> Optional[Dict[str, Any]]:
    """Valid breakout from a compression: strong-bodied close beyond the prior
    20-candle range (which was tight vs this symbol's own history) with
    above-average volume, still outside the range now."""
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
    """DEAD (untradable), QUIET (compressed: only a valid breakout may trade),
    NORMAL, WILD (too volatile)."""
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
    """Optional confirmation layer (EMA, RSI, MACD on 15m; EMA on 1H). Price action stays
    primary. Each check is reported; they block only when INDICATOR_FILTER=1."""
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


def fib_info(D: Dict[str, Dict[str, Any]], side: str, price: float) -> Dict[str, Any]:
    """Fibonacci layer on the 1H impulse: pullback zone (0.618-0.786), extension
    targets (1.272 / 1.618 from the pullback point) and overlap with 15m/5m OB/FVG.
    Always computed; affects decisions only when FIB_CONFIRM=1."""
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
