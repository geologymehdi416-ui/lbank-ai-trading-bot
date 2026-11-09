#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py (Part 1/5)
-----------------
Analysis-only crypto futures signal scanner (NO order execution).
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

LBANK_BASE = os.getenv("LBANK_BASE_URL", "https://api.lbkex.com").rstrip("/")
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT,"
                   "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,"
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
SCAN_LOG_FILE = os.getenv("SCAN_LOG_FILE", "scan_log.csv")
DIAG_FILE = os.getenv("DIAG_FILE", "diag_report.txt")

INDICATOR_FILTER = os.getenv("INDICATOR_FILTER", "0").strip() == "1"
FIB_CONFIRM = os.getenv("FIB_CONFIRM", "0").strip() == "1"
SWING_MIN_AMP = _env_float("SWING_MIN_AMP", 0.0)

MAX_STOP_PCT = 3.0
MAX_STOP_ATR = 3.0
MIN_STOP_ATR = 0.5
MIN_ATR_PCT = 0.0004
MAX_ATR_PCT = 0.03
MIN_VOL_RATIO = 0.5
MIN_CANDLES = 100

MIN_STOP_PCT = 0.25
FEE_PCT = _env_float("FEE_PCT", 0.06)
MIN_RR2_NET = 1.5
EXPIRY_H = _env_float("EXPIRY_H", 4.0)
TP_MIN_RR_NET = _env_float("TP_MIN_RR_NET", 0.5)
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
# Part 2/5: HTTP requests and Market Data Fetching
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
                time.sleep(min(delay, 30))
                continue
            raise ScanError("%s" % last_err)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network: %s" % (getattr(e, "reason", None) or e)
            time.sleep(min(2 ** attempt, 20))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON response"
            time.sleep(min(2 ** attempt, 20))
    raise ScanError("request failed after %d tries (%s)" % (retries, last_err))

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

def fetch_klines(symbol: str, tf: str, size: int = 300) -> Candles:
    typ, sec = TF_MAP[tf]
    now = int(time.time())
    start = now - size * sec
    data = http_json(LBANK_BASE + "/v2/kline.do",
                     {"symbol": symbol.lower(), "size": size, "type": typ, "time": start})
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
    except Exception:
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
# Part 3/5: Technical Analysis, Structure & Advanced SL/TP Calculation
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
    highs, lows = find_swings(h, l, 2 if name == "4h" else 3)
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

def calculate_levels_and_targets(cs: Candles, direction='LONG', entry_price=0):
    lookback = 50
    highs = cs.h[-lookback:]
    lows = cs.l[-lookback:]
    
    structural_low = min(lows)
    structural_high = max(highs)
    
    if direction == 'LONG':
        sl = structural_low * 0.992
        risk = entry_price - sl
        tp1 = entry_price + (risk * 1.5)
        tp2 = entry_price + (risk * 2.5)
        
        if structural_high > entry_price and structural_high > tp1:
            tp2 = structural_high
            
        reward1 = tp1 - entry_price
        net_rr1 = (reward1 / risk) if risk > 0 else 0
        
        reward2 = tp2 - entry_price
        net_rr2 = (reward2 / risk) if risk > 0 else 0
        
        return {
            "SL": round(sl, 4), "TP1": round(tp1, 4), "TP2": round(tp2, 4),
            "Net_RR1": round(net_rr1, 2), "Net_RR2": round(net_rr2, 2),
            "Valid": net_rr2 >= 1.5
        }
        
    elif direction == 'SHORT':
        sl = structural_high * 1.008
        risk = sl - entry_price
        tp1 = entry_price - (risk * 1.5)
        tp2 = entry_price - (risk * 2.5)
        
        if structural_low < entry_price and structural_low < tp1:
            tp2 = structural_low
            
        reward1 = entry_price - tp1
        net_rr1 = (reward1 / risk) if risk > 0 else 0
        
        reward2 = entry_price - tp2
        net_rr2 = (reward2 / risk) if risk > 0 else 0
        
        return {
            "SL": round(sl, 4), "TP1": round(tp1, 4), "TP2": round(tp2, 4),
            "Net_RR1": round(net_rr1, 2), "Net_RR2": round(net_rr2, 2),
            "Valid": net_rr2 >= 1.5
        }
# --------------------------------------------------------------------------
# Part 4/5: SMC Pattern Detection and Decision Engine
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

    sw15 = detect_sweep(d15, side, 12)
    sw5 = detect_sweep(d5, side, 24)
    chosen = None
    chosen_mss = None
    pairs = []
    if sw15:
        pairs.append((d15, sw15, 15))
    if sw5:
        pairs.append((d5, sw5, 8))
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
        notes.append("Liquidity Sweep در %s" % sweep["tf"])
    if chosen_mss:
        notes.append("MSS قوی تأیید شد (%s)" % chosen_mss[0]["tf"])
    elif aligned == 2:
        bos = detect_bos(d15, side, 6)
        if bos:
            parts["mss"] = 8.0
            notes.append("BOS ادامه‌دهنده در 15m")
            
    structure_ok = bool(sweep and chosen_mss) or bool(bos)
    if not structure_ok:
        soft.append("ستاپ ساختاری تأیید نشده")

    zone = pick_zone([d15, d5], side, price, atr15)
    if zone:
        entry = price if (zone["bottom"] <= price <= zone["top"]) else (zone["bottom"] + zone["top"]) / 2.0
        parts["zone"] = 15.0 if zone["kind"] == "OB+FVG" else 10.0
        notes.append("ناحیه ورود %s" % zone["kind"])
    else:
        entry = price
        parts["zone"] = 3.0 if structure_ok else 0.0

    levels_data = calculate_levels_and_targets(d15["cs"], direction=side, entry_price=entry)
    sl = levels_data["SL"]
    tp1 = levels_data["TP1"]
    tp2 = levels_data["TP2"]
    rr1n = levels_data["Net_RR1"]
    rr2n = levels_data["Net_RR2"]
    risk = abs(entry - sl)
    risk_pct = risk / entry * 100.0

    trade = None
    if risk_pct > MAX_STOP_PCT:
        blockers.append("فاصله حد ضرر بیش از حد مجاز است (%.2f%%)" % risk_pct)
    elif not levels_data["Valid"]:
        blockers.append("RR خالص بعد از کارمزد کمتر از حد نصاب است (%.2f)" % rr2n)
    else:
        cost = 2.0 * FEE_PCT
        notional_mult = (RISK_PCT / 100.0) / ((risk_pct + cost) / 100.0)
        lev = min(MAX_LEVERAGE, notional_mult)
        trade = {
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
            "rr1": abs(tp1 - entry) / risk, "rr2": abs(tp2 - entry) / risk,
            "rr1n": rr1n, "rr2n": rr2n, "cost_pct": cost,
            "stop_pct": risk_pct, "order_type": "LIMIT",
            "notional_x": lev, "eff_risk_pct": lev * ((risk_pct + cost) / 100.0) * 100.0,
            "capped": notional_mult > MAX_LEVERAGE, "tp2_ok": True,
        }

    parts["room"] = 5.0
    parts["rr"] = 10.0 if rr2n >= 2.0 else 6.0
    parts["momentum"] = 10.0

    score = int(round(sum(parts.values())))
    if not structure_ok:
        score = min(score, STRUCT_CAP)
        
    return {"side": side, "score": score, "parts": parts, "notes": notes,
            "blockers": blockers, "soft_blockers": soft, "trade": trade,
            "zone": zone, "sweep": sweep, "fib": {"valid": False}, "ind": {}}
# --------------------------------------------------------------------------
# Part 5/5: Reporting, Telegram Delivery and Main Execution
# --------------------------------------------------------------------------
def make_watch(res: Dict[str, Any], sc: Dict[str, Any], extra: str = "") -> Dict[str, Any]:
    return {"side": sc["side"], "score": sc["score"], "trade": sc["trade"],
            "condition": "منتظر تأیید کامل ساختار و شرایط بازار", "extra": extra}

def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    price = fetch_price(symbol) or C["5m"].c[-1]
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    regime = classify_regime(D, price)
    gb = global_blockers(D, price, regime)
    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    
    signal = None
    for s in sorted(scenarios.values(), key=lambda x: -x["score"]):
        if s["score"] >= min_score and not s["blockers"] and not gb and s["trade"]:
            signal = s
            break
            
    age_min = (time.time() - (D["5m"].get("cs", C["5m"]).t[-1] + TF_MAP["5m"][1])) / 60.0
    res: Dict[str, Any] = {
        "symbol": symbol, "price": price, "D": D, "scenarios": scenarios,
        "global_blockers": gb, "signal": signal, "watch": None,
        "reason_override": None, "data_age_min": age_min, "regime": regime,
        "liq": liquidity_map(D["15m"], price),
    }
    if not signal and not gb:
        cands = [s for s in scenarios.values() if s["trade"] and not s["blockers"] and s["score"] >= WATCH_MIN]
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
    ls, ss = res["scenarios"]["LONG"], res["scenarios"]["SHORT"]
    dom = "SHORT" if ss["score"] >= ls["score"] else "LONG"
    short = dom == "SHORT"
    sc = ss if short else ls
    price = res["price"]
    t = sc["trade"]
    missing = list(res.get("global_blockers", [])) + list(sc["soft_blockers"]) + list(sc["blockers"])
    if sc["score"] < MIN_SCORE:
        missing.append("امتیاز %d از %d" % (sc["score"], MIN_SCORE))
    decision = "ENTER" if (res.get("signal") and res["signal"]["side"] == dom) else ("CONDITIONAL" if t else "NO_ENTRY")
    return {"side": dom, "status": "سیگنال صادر شد" if decision == "ENTER" else ("در انتظار: " + (missing[0] if missing else "شرایط")),
            "trade": t, "missing": missing, "short": short, "decision": decision}

def plan_fields(res: Dict[str, Any]) -> Dict[str, Any]:
    c = build_card(res)
    t = c["trade"]
    short = c["short"]
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
    f.update({
        "activation": t["entry"],
        "confirmation": "تأیید کندل 15m هم‌جهت",
        "zone": (t["entry"] * 0.999, t["entry"] * 1.001),
        "invalidation": "حد ضرر ساختاری",
        "sl": t["sl"], "tp1": t["tp1"], "tp2": t["tp2"],
        "rr1": t["rr1n"], "rr2": t["rr2n"], "leverage": t["notional_x"],
    })
    return f

def format_brief(res: Dict[str, Any]) -> List[str]:
    p = plan_fields(res)
    f = fmt_price
    D = res["D"]
    side_key = "LONG" if p["side"] == "LONG" else "SHORT"
    sc = res["scenarios"][side_key]
    read = [n for n in sc["notes"] if not n.startswith("حجم")][:3]
    lines = [
        "<b>%s</b> | %s | %s | انقضا: %s" % (html.escape(res["symbol"]), p["side"],
                                          p["decision"], html.escape(p["expiry"])),
        "روند 4H/1H/15m: %s %s %s" % (TREND_ARROW[D["4h"]["trend"]], TREND_ARROW[D["1h"]["trend"]],
                                     TREND_ARROW[D["15m"]["trend"]]),
    ]
    if read:
        lines.append("تحلیل: " + html.escape(" | ".join(read)))
    if p["trade"] is None:
        lines.append("طرح معامله: — (تارگت معتبر نیست)")
    else:
        lines.append("Activation: %s | Entry Zone: %s – %s" % (
            f(p["activation"]), f(p["zone"][0]), f(p["zone"][1])))
        lines.append("SL: %s (%.2f%%) | TP1: %s | TP2: %s" % (
            f(p["sl"]), p["trade"]["stop_pct"], f(p["tp1"]), f(p["tp2"])))
        lines.append("Net RR: TP1 %.2f / TP2 %.2f | Leverage ≤ %.2fx" % (p["rr1"], p["rr2"], p["leverage"]))
    if p["decision"] != DECISION_FA["ENTER"] and p["missing"]:
        lines.append("نواقص: " + html.escape(" | ".join(p["missing"][:2])))
    return lines

def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]],
                  sent_signals: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M (UTC+03:30)")
    sigs = [r for r in results if r["signal"]]
    lines = ["📊 <b>گزارش اسکن</b> — %s" % now]
    if sigs:
        lines.append("🎯 سیگنال قطعی: %d" % len(sigs))
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

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = []
    errors = []
    pairs = fetch_pairs()
    if pairs:
        for sym in [x for x in symbols if x.lower() not in pairs]:
            errors.append((sym, "جفت‌ارز در اسپات LBank پیدا نشد؛ نادیده گرفته شد"))
        symbols = [x for x in symbols if x.lower() in pairs]
        
    for sym in symbols:
        try:
            results.append(analyze_symbol(sym, args.min_score))
        except ScanError as e:
            errors.append((sym, str(e)))
        except Exception as e:
            errors.append((sym, str(e)))

    report_text = format_report(results, errors, 0)
    
    report_file_path = os.getenv("REPORT_FILE", "report.txt")
    try:
        with open(report_file_path, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print(f"Successfully generated {report_file_path}")
    except OSError as e:
        print(f"Failed to write report file: {e}")

    if args.dry_run:
        print(report_text)
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
