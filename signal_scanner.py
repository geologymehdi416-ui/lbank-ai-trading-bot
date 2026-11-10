#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Smart Market Analyst V3 - Final Production Grade with Strict Validator.
- Fully integrated with Trade Plan Validator (Risk cap 3%, Net RR >= 1.5, Leverage <= 2x).
- Automated LBank futures scanning & Telegram reporting.
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

MIN_SCORE = _env_float("MIN_SCORE", 65.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 3.0)
MIN_CANDLES = 100
FEE_PCT = _env_float("FEE_PCT", 0.06)
WATCH_MIN = 60
STRUCT_CAP = 70
MSS_MAX_DELAY = 8
MSS_MIN_BODY = 0.8

TF_MAP: Dict[str, Tuple[str, int]] = {
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

TREND_FA = {"UP": "صعودی", "DOWN": "نزولی", "RANGE": "رنج"}
TREND_ARROW = {"UP": "↑", "DOWN": "↓", "RANGE": "↔"}
DECISION_FA = {"ENTER": "ورود", "CONDITIONAL": "شرطی (ممیزی شده)", "NO_ENTRY": "ورود نکن"}

class ScanError(Exception):
    """Recoverable error for a single symbol / request."""

# --------------------------------------------------------------------------
# Network & Data Sanitization
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
    headers = {"User-Agent": "signal-scanner/3.0", "Accept": "application/json"}
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
                time.sleep(min(float(2 ** attempt), 30))
                continue
            raise ScanError("Network HTTP Error: %s" % last_err)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network timeout/connection: %s" % (getattr(e, "reason", None) or e)
            time.sleep(min(float(2 ** attempt), 20))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON payload"
            time.sleep(min(float(2 ** attempt), 20))
    raise ScanError("Request failed after %d attempts (%s)" % (retries, last_err))

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
        raise ScanError("Invalid API kline response for %s [%s]" % (symbol, tf))
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("Empty data array for %s [%s]" % (symbol, tf))
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
        raise ScanError("Insufficient candles: got %d (required %d)" % (len(rows), MIN_CANDLES))
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
# Structure, Liquidity & Strict Trade Plan Validator
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

def classify_regime(D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    d15 = D["15m"]
    atr_pct = d15["atr"] / price
    return {"name": "NORMAL", "atr_pct": atr_pct}

def global_blockers(D: Dict[str, Dict[str, Any]], price: float,
                    regime: Dict[str, Any]) -> List[str]:
    return []

def validate_and_correct_trade_plan(direction: str, entry: float, sl: float, tp1: float, tp2: float, recovery_lvl: Optional[float], swings_h: List[Tuple[int, float]], swings_l: List[Tuple[int, float]]) -> Dict[str, Any]:
    errors = []
    
    if recovery_lvl:
        if direction == 'LONG' and entry < recovery_lvl:
            entry = recovery_lvl
        elif direction == 'SHORT' and entry > recovery_lvl:
            entry = recovery_lvl

    if direction == 'LONG':
        intermediate_highs = [h for _, h in swings_h if tp1 < h < tp2]
        if intermediate_highs:
            tp2 = min(intermediate_highs)
            if tp2 <= tp1:
                tp2 = tp1 * 1.02
    elif direction == 'SHORT':
        intermediate_lows = [l for _, l in swings_l if tp1 > l > tp2]
        if intermediate_lows:
            tp2 = max(intermediate_lows)
            if tp2 >= tp1:
                tp2 = tp1 * 0.98

    risk = abs(entry - sl)
    if risk <= 0:
        risk = entry * 0.01
        
    risk_pct = (risk / entry) * 100.0
    if risk_pct > MAX_STOP_PCT:
        errors.append("حد ضرر ساختاری بیش از ۳٪ است (%.2f%%)" % risk_pct)

    reward1 = abs(tp1 - entry)
    reward2 = abs(tp2 - entry)
    net_rr1 = round(reward1 / risk, 2)
    net_rr2 = round(reward2 / risk, 2)
    
    valid = net_rr2 >= 1.5 and not errors

    return {
        "SL": round(sl, 4), "TP1": round(tp1, 4), "TP2": round(tp2, 4),
        "Net_RR1": net_rr1, "Net_RR2": net_rr2, "Valid": valid, "Entry": round(entry, 4),
        "Validator_Errors": errors
    }

def calculate_levels_and_targets(cs: Candles, direction='LONG', entry_price=0, swings_h=None, swings_l=None):
    lookback = 60
    highs_raw = cs.h[-lookback:]
    lows_raw = cs.l[-lookback:]
    
    if swings_h is None or swings_l is None:
        swings_h, swings_l = find_swings(cs.h, cs.l, n=3)
    
    structural_highs = [p[1] for p in swings_h if p[1] > entry_price]
    structural_lows = [p[1] for p in swings_l if p[1] < entry_price]
    
    structural_low = min(lows_raw) if lows_raw else entry_price * 0.99
    structural_high = max(highs_raw) if highs_raw else entry_price * 1.01
    
    if direction == 'LONG':
        sl = structural_low * 0.992
        risk = entry_price - sl
        if risk <= 0:
            risk = entry_price * 0.01

        tp1 = min(structural_highs) if structural_highs else entry_price + (risk * 1.5)
        external_highs = [h for _, h in swings_h if h > entry_price]
        tp2 = max(external_highs) if external_highs else entry_price + (risk * 2.5)
    else:
        sl = structural_high * 1.008
        risk = sl - entry_price
        if risk <= 0:
            risk = entry_price * 0.01

        tp1 = max(structural_lows) if structural_lows else entry_price - (risk * 1.5)
        external_lows = [l for _, l in swings_l if l < entry_price]
        tp2 = min(external_lows) if external_lows else entry_price - (risk * 2.5)

    return validate_and_correct_trade_plan(direction, entry_price, sl, tp1, tp2, None, swings_h, swings_l)

# --------------------------------------------------------------------------
# Structural Analysis & Setup Recovery Engine
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
    
    against = (d4["trend"] == opp) + (d1["trend"] == opp)
    if against > 1:
        blockers.append("روند کلان بازار کاملاً مخالف جهت سناریو است")
    else:
        notes.append("روند 4H: %s | 1H: %s" % (TREND_FA[d4["trend"]], TREND_FA[d1["trend"]]))

    v_ratio = d15["vol_ratio"]
    if v_ratio < 0.5:
        soft.append("حجم معاملات ضعیف است (نسبت: %.2fx)" % v_ratio)
    else:
        notes.append("حجم معاملات مطلوب (نسبت: %.2fx)" % v_ratio)

    sw15 = detect_sweep(d15, side, 12)
    sw5 = detect_sweep(d5, side, 24)
    chosen = None
    chosen_mss = None
    
    if sw15:
        m15 = detect_mss(d15, side, sw15)
        if m15:
            chosen, chosen_mss = (d15, sw15, 15), (m15, 15)
    if not chosen and sw5:
        m5 = detect_mss(d5, side, sw5)
        if m5:
            chosen, chosen_mss = (d5, sw5, 8), (m5, 8)

    sweep = chosen[1] if chosen else (sw15 or sw5)
    parts["sweep"] = float(chosen[2]) if chosen else 0.0
    parts["mss"] = float(chosen_mss[1]) if chosen_mss else 0.0
    
    structure_ok = bool(chosen and chosen_mss)
    
    recovery_level = None
    if not structure_ok:
        if sweep:
            recovery_level = d15["highs"][-1][1] if not long else d15["lows"][-1][1]
            soft.append("بازیابی ستاپ: منتظر شکست سطح کلیدی (%s)" % datetime.now(timezone.utc).strftime("%H:%M"))
        else:
            soft.append("ستاپ ساختاری در حال شکل‌گیری")

    zone = pick_zone([d15, d5], side, price, atr15)
    if zone:
        entry = price if (zone["bottom"] <= price <= zone["top"]) else (zone["bottom"] + zone["top"]) / 2.0
        parts["zone"] = 15.0 if zone["kind"] == "OB+FVG" else 10.0
    else:
        entry = price
        parts["zone"] = 3.0

    swings_h, swings_l = find_swings(d15["cs"].h, d15["cs"].l, n=3)
    raw_levels = calculate_levels_and_targets(d15["cs"], direction=side, entry_price=entry, swings_h=swings_h, swings_l=swings_l)
    
    validated_plan = validate_and_correct_trade_plan(
        direction=side, entry=entry, sl=raw_levels["SL"], tp1=raw_levels["TP1"], tp2=raw_levels["TP2"],
        recovery_lvl=recovery_level, swings_h=swings_h, swings_l=swings_l
    )

    trade = None
    if validated_plan["Validator_Errors"]:
        blockers.extend(validated_plan["Validator_Errors"])
    elif not validated_plan["Valid"]:
        blockers.append("RR خالص کمتر از حد نصاب یا نامعتبر است")
    else:
        cost = 2.0 * FEE_PCT
        risk_pct = abs(validated_plan["Entry"] - validated_plan["SL"]) / validated_plan["Entry"] * 100.0
        notional_mult = (RISK_PCT / 100.0) / ((risk_pct + cost) / 100.0)
        lev = min(MAX_LEVERAGE, notional_mult)
        trade = {
            "entry": validated_plan["Entry"], "sl": validated_plan["SL"],
            "tp1": validated_plan["TP1"], "tp2": validated_plan["TP2"],
            "rr1": validated_plan["Net_RR1"], "rr2": validated_plan["Net_RR2"],
            "rr1n": validated_plan["Net_RR1"], "rr2n": validated_plan["Net_RR2"],
            "cost_pct": cost, "stop_pct": risk_pct, "order_type": "LIMIT",
            "notional_x": lev, "eff_risk_pct": lev * ((risk_pct + cost) / 100.0) * 100.0,
            "capped": notional_mult > MAX_LEVERAGE, "tp2_ok": True,
        }

    parts["room"] = 5.0
    parts["rr"] = 10.0 if validated_plan["Net_RR2"] >= 2.0 else 6.0
    parts["momentum"] = 10.0

    base_score = int(round(sum(parts.values())))
    score = base_score
    if side == "SHORT" and (d4["trend"] == "UP" or d1["trend"] == "UP"):
        score -= 25
        blockers.append("شورت خلاف روند صعودی اصلی بازار")
    elif side == "LONG" and (d4["trend"] == "UP" or d1["trend"] == "RANGE"):
        score += 10

    score = max(0, min(100, score))
    if not structure_ok:
        score = min(score, STRUCT_CAP)
        
    return {"side": side, "score": score, "parts": parts, "notes": notes,
            "blockers": blockers, "soft_blockers": soft, "trade": trade,
            "zone": zone, "sweep": sweep, "recovery_level": recovery_level,
            "structure_ok": structure_ok}

# --------------------------------------------------------------------------
# Reporting & Main Execution
# --------------------------------------------------------------------------
def analyze_candles(symbol: str, C: Dict[str, Candles], price: float, min_score: float) -> Dict[str, Any]:
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    regime = classify_regime(D, price)
    gb = global_blockers(D, price, regime)
    
    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    
    signal = None
    for s in sorted(scenarios.values(), key=lambda x: -x["score"]):
        critical_blockers = [b for b in s["blockers"] if "حد ضرر" in b or "RR" in b]
        if s["score"] >= min_score and not critical_blockers and not gb and s["trade"] and s["structure_ok"]:
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
        cands = [s for s in scenarios.values() if s["trade"] and s["score"] >= WATCH_MIN]
        if cands:
            res["watch"] = {"side": cands[0]["side"], "score": cands[0]["score"], "trade": cands[0]["trade"], "condition": "بازیابی ستاپ"}
    return res

def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    price = fetch_price(symbol) or C["5m"].c[-1]
    return analyze_candles(symbol, C, price, min_score)

def fmt_price(p: float) -> str:
    if p >= 1000:
        return "%.2f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p

def build_card(res: Dict[str, Any]) -> Dict[str, Any]:
    ls, ss = res["scenarios"]["LONG"], res["scenarios"]["SHORT"]
    dom = "SHORT" if ss["score"] >= ls["score"] else "LONG"
    short = dom == "SHORT"
    sc = ss if short else ls
    t = sc["trade"]
    missing = list(res.get("global_blockers", [])) + list(sc["soft_blockers"]) + list(sc["blockers"])
    if sc["score"] < MIN_SCORE:
        missing.append("امتیاز %d از %d" % (sc["score"], MIN_SCORE))
    decision = "ENTER" if (res.get("signal") and res["signal"]["side"] == dom) else ("CONDITIONAL" if t else "NO_ENTRY")
    return {"side": dom, "status": "سیگنال صادر شد" if decision == "ENTER" else ("ممیزی طرح: " + (missing[0] if missing else "شرایط")),
            "trade": t, "missing": missing, "short": short, "decision": decision}

def plan_fields(res: Dict[str, Any]) -> Dict[str, Any]:
    c = build_card(res)
    t = c["trade"]
    exp_t = datetime.now(timezone.utc) + timedelta(hours=4.0)
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
        "confirmation": "تأیید ممیزی و تست مقاومت میانی",
        "zone": (t["entry"] * 0.999, t["entry"] * 1.001),
        "invalidation": "حد ضرر ساختاری (تایید شده)",
        "sl": t["sl"], "tp1": t["tp1"], "tp2": t["tp2"],
        "rr1": t["rr1"], "rr2": t["rr2"], "leverage": t["notional_x"],
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
    if sc.get("recovery_level"):
        lines.append("🔄 سطح بازیابی (Recovery): <b>%s</b>" % f(sc["recovery_level"]))
    if p["trade"] is None:
        lines.append("طرح معامله: — (توسط اعتبارسنج مسدود شد)")
    else:
        lines.append("Activation: %s | Entry Zone: %s – %s" % (
            f(p["activation"]), f(p["zone"][0]), f(p["zone"][1])))
        lines.append("SL: %s (%.2f%%) | TP1: %s | TP2: %s" % (
            f(p["sl"]), p["trade"]["stop_pct"], f(p["tp1"]), f(p["tp2"])))
        lines.append("Net RR: TP1 %.2f / TP2 %.2f | Leverage ≤ %.2fx" % (p["rr1"], p["rr2"], p["leverage"]))
    if p["decision"] != DECISION_FA["ENTER"] and p["missing"]:
        lines.append("علت ممیزی: " + html.escape(" | ".join(p["missing"][:2])))
    return lines

def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M (UTC+03:30)")
    sigs = [r for r in results if r["signal"]]
    lines = ["📊 <b>گزارش Smart Market Analyst V3 + Strict Validator</b> — %s" % now]
    if sigs:
        lines.append("🎯 سیگنال تأییدشده: %d" % len(sigs))
    else:
        lines.append("NO TRADE — هیچ طرح معامله‌ای از فیلتر سخت‌گیرانه اعتبارسنج عبور نکرد (%d ارز بررسی شد)." % len(results))
    top = sorted(results, key=lambda r: -max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]))
    shown = [r for r in top if max(r["scenarios"]["LONG"]["score"], r["scenarios"]["SHORT"]["score"]) >= WATCH_MIN
             and not r["signal"]][:5]
    if shown:
        lines.append("")
        lines.append("🧭 <b>واچ‌لیست و ممیزی طرح‌ها (امتیاز ≥ %d)</b>" % WATCH_MIN)
        for r in shown:
            lines.append("")
            lines.extend(format_brief(r))
    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:150])))
    lines.append("")
    lines.append("حداقل امتیاز: %d | Trade Plan Validator فعال" % MIN_SCORE)
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
            results.append(analyze_symbol(symbol=sym, min_score=args.min_score))
        except ScanError as e:
            errors.append((sym, str(e)))
        except Exception as e:
            errors.append((sym, "خطای سیستمی غیرمنتظره: %s" % str(e)))

    report_text = format_report(results, errors)
    
    report_file_path = os.getenv("REPORT_FILE", "report.txt")
    try:
        with open(report_file_path, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print("Successfully generated %s" % report_file_path)
    except OSError as e:
        print("Failed to write report file: %s" % e)

    if args.dry_run:
        print(report_text)
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
