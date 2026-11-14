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
  MAX_TP_RR          targets with net RR above this are unrealistic and skipped, default 5
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
MAX_TP_RR = _env_float("MAX_TP_RR", 5.0)  # targets with net RR above this are unrealistic and skipped
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


def global_blockers(D: Dict[str, Dict[str, Any]], price: float,
                    regime: Dict[str, Any]) -> List[str]:
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
    return {"checks": checks, "passed": passed, "total": len(checks), "failed": failed,
            "ok": passed >= 3, "rsi": rsi, "ema20": e20, "ema50": e50}


def fib_info(D: Dict[str, Dict[str, Any]], side: str, price: float) -> Dict[str, Any]:
    """Fibonacci layer on the 1H impulse: pullback zone (0.618-0.786), extension
    targets (1.272 / 1.618 from the pullback point) and overlap with 15m/5m OB/FVG.
    Always computed; affects decisions only when FIB_CONFIRM=1."""
    long = side == "LONG"
    out: Dict[str, Any] = {"valid": False, "retr": None, "zone": None, "targets": [],
                           "overlap": False, "in_zone": False, "pts": 0}
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
    overlap = any(max(z["bottom"], zlo) < min(z["top"], zhi)
                  for z in _zones_for(d15, side) + _zones_for(d5, side))
    in_zone = (zlo - 0.25 * atr15) <= price <= (zhi + 0.25 * atr15)
    pts = 0
    if valid and in_zone:
        pts = FIB_PTS_OVERLAP if overlap else FIB_PTS_ZONE
    out.update({"valid": valid, "retr": retr, "zone": zone, "targets": targets,
                "overlap": overlap, "in_zone": in_zone, "pts": pts,
                "impulse": (ap, bp, cp, diff)})
    return out


def fib_plan(D: Dict[str, Dict[str, Any]], side: str, price: float) -> Optional[Dict[str, Any]]:
    """Waiting plan from the 1H impulse (planning only, never creates a signal).
    Entry at the 0.618 pullback; stop beyond the 0.786 level plus 0.25 ATR(15m);
    TP1 = prior structural extreme of the impulse, TP2 = 1.272 extension of the impulse.
    Each target must have positive net RR above TP_MIN_RR_NET."""
    f = fib_info(D, side, price)
    if not f.get("impulse") or not f["valid"]:
        return None
    long = side == "LONG"
    a, b, c, diff = f["impulse"]
    atr15 = D["15m"]["atr"]
    zlo, zhi = f["zone"]
    if long:
        entry = b - 0.618 * diff
        deep = b - 0.786 * diff
        sl = deep - 0.25 * atr15
        cand = [b, a + 1.272 * diff]
    else:
        entry = b + 0.618 * diff
        deep = b + 0.786 * diff
        sl = deep + 0.25 * atr15
        cand = [b, a - 1.272 * diff]
    if (long and not sl < entry) or ((not long) and not sl > entry):
        return None
    risk_pct = abs(sl - entry) / entry * 100.0
    cost = 2.0 * FEE_PCT
    tps: List[Tuple[float, float]] = []
    for t in cand:
        on_side = t > entry if long else t < entry
        if not on_side:
            tps.append((None, None))
            continue
        rr = (abs(t - entry) / entry * 100.0 - cost) / (risk_pct + cost)
        tps.append((t, rr) if rr >= TP_MIN_RR_NET else (None, None))
    margin = 0.25 * atr15
    if long:
        if price > zhi + margin:
            status = "منتظر پولبک به ۰.۶۱۸ (فاصله %.2f%%)" % ((price - entry) / entry * 100.0)
        elif price >= zlo - margin:
            status = "در ناحیه‌ی ۰.۶۱۸ تا ۰.۷۸۶: منتظر تأیید 5m (MSS + کندل بدنه‌دار هم‌جهت)"
        else:
            status = "پولبک عمیق‌تر از ۰.۷۸۶: طرح باطل"
    else:
        if price < zlo - margin:
            status = "منتظر پولبک به ۰.۶۱۸ (فاصله %.2f%%)" % ((entry - price) / entry * 100.0)
        elif price <= zhi + margin:
            status = "در ناحیه‌ی ۰.۶۱۸ تا ۰.۷۸۶: منتظر تأیید 5m (MSS + کندل بدنه‌دار هم‌جهت)"
        else:
            status = "پولبک عمیق‌تر از ۰.۷۸۶: طرح باطل"
    return {"entry": entry, "sl": sl, "stop_pct": risk_pct,
            "tp1": tps[0][0], "tp1_rr": tps[0][1], "tp2": tps[1][0], "tp2_rr": tps[1][1],
            "zone": (zlo, zhi), "status": status}


def pick_targets(levels: List[float], entry: float, risk: float, long: bool,
                 fee_pct: float) -> List[Tuple[float, float]]:
    """Nearest structural levels in the trade direction whose NET reward/risk is positive.
    The second target must sit at least 0.5R beyond the first (no near-duplicate TP2).
    Returns up to two (price, rr_net)."""
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
    if chosen and detect_sweep(d1, side, 4):
        parts["sweep"] = min(15.0, parts["sweep"] + 5.0)
        notes.append("Sweep هم‌زمان روی نقدینگی 1H")
    bos = None
    if sweep:
        notes.append("Liquidity Sweep در %s %s" % (sweep["tf"], "(نقدینگی فروش)" if long else "(نقدینگی خرید)"))
    if chosen_mss:
        notes.append("MSS قوی تأیید شد (%s)" % chosen_mss[0]["tf"])
        lvl_mss = chosen_mss[0]["level"]
        moved = (price - lvl_mss) if long else (lvl_mss - price)
        if moved > 2.0 * atr15:
            blockers.append("ورود دیرهنگام: قیمت بیش از 2 ATR از سطح MSS دور شده")
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
        blockers.append("فیلتر اندیکاتور: %d از %d تأیید (ناموفق: %s)" % (
            ind["passed"], ind["total"], "، ".join(ind["failed"])))

    zone = pick_zone([d15, d5], side, price, atr15)
    if zone:
        inside = zone["bottom"] <= price <= zone["top"]
        mid = (zone["bottom"] + zone["top"]) / 2.0
        entry = price if inside else mid
        entry = min(entry, price) if long else max(entry, price)
        parts["zone"] = 15.0 if zone["kind"] == "OB+FVG" else 10.0 if zone["kind"] == "OB" else 9.0
        notes.append("ناحیه ورود %s در %s" % (zone["kind"], zone["tf"]))
    else:
        entry = price
        parts["zone"] = 3.0 if structure_ok else 0.0

    refs: List[float] = []
    if zone:
        refs.append(zone["bottom"] if long else zone["top"])
    if sweep:
        refs.append(sweep["extreme"])
    if not refs:
        side_pts = [p for _, p in (d15["lows"] if long else d15["highs"])
                    if (p < entry if long else p > entry)]
        if side_pts:
            refs.append(side_pts[-1])
    ref = (min(refs) if long else max(refs)) if refs else (entry - 1.5 * atr15 if long else entry + 1.5 * atr15)
    if (long and ref >= entry) or (not long and ref <= entry):
        ref = entry - 1.5 * atr15 if long else entry + 1.5 * atr15
    buf = 0.25 * atr15
    sl = ref - buf if long else ref + buf
    risk = abs(entry - sl)
    min_risk = max(MIN_STOP_ATR * atr15, MIN_STOP_PCT / 100.0 * entry, 0.2 * d1["atr"])
    if risk < min_risk:
        risk = min_risk
        sl = entry - risk if long else entry + risk
        notes.append("حد ضرر به حداقل مجاز گسترش یافت (%.2f%%)" % (risk / entry * 100.0))
    draft = {"entry": entry, "sl": sl, "stop_pct": risk / entry * 100.0}
    trade: Optional[Dict[str, Any]] = None
    if risk > MAX_STOP_ATR * atr15 or risk / entry * 100.0 > MAX_STOP_PCT:
        blockers.append("فاصله حد ضرر بیش از حد مجاز است (%.2f%%)" % (risk / entry * 100.0))
    else:
        levels = collect_levels(D)
        if FIB_CONFIRM and fib["valid"]:
            levels = levels + [p for _, p in fib["targets"]]
        targets = pick_targets(levels, entry, risk, long, FEE_PCT)
        risk_pct = risk / entry * 100.0
        cost = 2.0 * FEE_PCT
        total_stop = (risk_pct + cost) / 100.0
        notional_mult = (RISK_PCT / 100.0) / total_stop
        lev = min(MAX_LEVERAGE, notional_mult)
        eff_risk = lev * total_stop * 100.0
        order_type = "LIMIT" if abs(entry - price) > 0.05 * atr15 else "MARKET"
        if not targets:
            blockers.append("تارگت معتبر با RR خالص مثبت پیدا نشد")
        else:
            tp1, rr1n = targets[0]
            tp2, rr2n = targets[1] if len(targets) > 1 else (None, None)
            trade = {
                "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                "rr1": abs(tp1 - entry) / risk,
                "rr2": (abs(tp2 - entry) / risk) if tp2 is not None else None,
                "rr1n": rr1n, "rr2n": rr2n, "cost_pct": cost,
                "stop_pct": risk_pct, "order_type": order_type,
                "notional_x": lev, "eff_risk_pct": eff_risk, "capped": notional_mult > MAX_LEVERAGE,
                "tp2_ok": tp2 is not None,
            }
            if tp2 is None:
                blockers.append("تارگت دوم معتبر پیدا نشد")
            elif rr2n < MIN_RR2_NET:
                blockers.append("RR خالص بعد از کارمزد کمتر از %.1f است (%.2f)" % (MIN_RR2_NET, rr2n))
        htf = collect_levels(D, htf_only=True)
        obst = [p for p in htf if (p > entry if long else p < entry)]
        if obst:
            nearest = min(obst) if long else max(obst)
            room_r = abs(nearest - entry) / risk
        else:
            room_r = 99.0
        parts["room"] = 5.0 if room_r >= 2.0 else 3.0 if room_r >= 1.2 else 0.0
        if room_r < 1.0:
            blockers.append("مانع HTF نزدیک (کمتر از 1R تا هدف)")
        elif room_r < 1.2:
            notes.append("مانع HTF نسبتاً نزدیک است")
    parts.setdefault("room", 0.0)
    if trade and trade["rr2n"] is not None:
        rr = trade["rr2n"]
        parts["rr"] = 10.0 if rr >= 2.5 else 8.0 if rr >= 2.0 else 6.0 if rr >= 1.5 else 3.0 if rr >= 1.0 else 0.0
    else:
        parts["rr"] = 0.0

    cs15: Candles = d15["cs"]
    n15 = len(cs15)
    sgn = 1.0 if long else -1.0
    mom = 0.0
    if any(sgn * (cs15.c[k] - cs15.o[k]) >= 0.8 * atr15 for k in range(n15 - 6, n15)):
        mom += 6
    if sum(sgn * (cs15.c[k] - cs15.o[k]) for k in range(n15 - 3, n15)) >= 0.3 * atr15:
        mom += 4
    if d5["struct"] == (1 if long else -1):
        mom += 5
    if FIB_CONFIRM:
        mom = min(mom, 10.0)
    parts["momentum"] = mom
    if not (d5["struct"] == (1 if long else -1) and mom >= 10):
        soft.append("تأیید ساختار 5m و مومنتوم کافی نیست")
    parts["fib"] = float(fib["pts"]) if FIB_CONFIRM else 0.0
    if mom >= 10:
        notes.append("مومنتوم پرایس‌اکشن: جابه‌جایی هم‌جهت و ساختار 5m هم‌راستا")
    if d15["vol_ratio"] >= 1.2:
        notes.append("حجم بالاتر از میانگین (x%.2f)" % d15["vol_ratio"])

    score = int(round(sum(parts.values())))
    if not structure_ok:
        score = min(score, STRUCT_CAP)
    return {"side": side, "score": score, "parts": parts, "notes": notes,
            "blockers": blockers, "soft_blockers": soft, "trade": trade,
            "zone": zone, "sweep": sweep, "fib": fib, "ind": ind, "draft": draft}
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
            sig
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
        new_file = (not os.path.exi