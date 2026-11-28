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
    highs, lows = find_swings(h, l, n_swing)
    st = structure_dir(highs, lows)
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
        "atr_list": at,
    }


# --------------------------------------------------------------------------
# Liquidity Hunt, OB/FVG & Active Chart Leg
# --------------------------------------------------------------------------
def detect_sweep(d: Dict[str, Any], side: str, lookback: int = 20) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    a = d["atr"]
    long = side == "LONG"
    pts = [p for p in (d["lows"] if long else d["highs"]) if p[0] >= n - 100]
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


def find_fvgs(cs: Candles, atr_v: float, side: str, lookback: int = 40) -> List[Dict[str, float]]:
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


def find_obs(cs: Candles, atr_v: float, side: str, lookback: int = 50) -> List[Dict[str, float]]:
    n = len(cs)
    out: List[Dict[str, float]] = []
    for i in range(max(1, n - lookback), n - 3):
        if side == "LONG" and cs.c[i] < cs.o[i]:
            move = max(cs.h[i + 1:i + 4]) - cs.h[i]
            if move >= 0.75 * atr_v and min(cs.c[i + 1:]) > cs.l[i]:
                out.append({"bottom": cs.l[i], "top": cs.o[i]})
        elif side == "SHORT" and cs.c[i] > cs.o[i]:
            move = cs.l[i] - min(cs.l[i + 1:i + 4])
            if move >= 0.75 * atr_v and max(cs.c[i + 1:]) < cs.h[i]:
                out.append({"bottom": cs.o[i], "top": cs.h[i]})
    return out


def _zones_for(d: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    zs: List[Dict[str, Any]] = []
    for z in find_fvgs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "FVG", "tf": d["name"]})
    for z in find_obs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "OB", "tf": d["name"]})
    return zs


def pick_active_zone(
    ds: List[Dict[str, Any]], side: str, price: float, atr15: float
) -> Optional[Dict[str, Any]]:
    """Finds the nearest valid OB, FVG, or OB+FVG overlap on the active chart."""
    long = side == "LONG"
    allz: List[Dict[str, Any]] = []
    for d in ds:
        allz.extend(_zones_for(d, side))

    def dist(z: Dict[str, Any]) -> float:
        if z["bottom"] <= price <= z["top"]:
            return 0.0
        return max(0.0, price - z["top"]) if long else max(0.0, z["bottom"] - price)

    def valid(z: Dict[str, Any]) -> bool:
        if z["top"] <= z["bottom"]:
            return False
        if long:
            return z["top"] <= price + 0.15 * atr15 and z["bottom"] < price and dist(z) <= 2.2 * atr15
        return z["bottom"] >= price - 0.15 * atr15 and z["top"] > price and dist(z) <= 2.2 * atr15

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


def active_chart_leg(d: Dict[str, Any], price: float) -> Optional[Dict[str, Any]]:
    """Extracts the LIVE active swing leg (A -> B) currently containing or adjacent to price,
    so Fibonacci levels match what the trader actually sees on the current chart."""
    cs: Candles = d["cs"]
    n = len(cs)
    start = max(0, n - ACTIVE_SWING_BARS)
    hs = [p for p in d["highs"] if p[0] >= start]
    ls = [p for p in d["lows"] if p[0] >= start]
    if not hs or not ls:
        return None

    hi_i, hi_v = max(hs, key=lambda x: x[1])
    lo_i, lo_v = min(ls, key=lambda x: x[1])
    diff = hi_v - lo_v
    if diff < 1.2 * d["atr"]:
        return None

    # Determine whether the active leg is bullish (low -> high) or bearish (high -> low)
    if lo_i < hi_i:
        direction = "UP"
        A, B = lo_v, hi_v
    else:
        direction = "DOWN"
        A, B = hi_v, lo_v

    return {"dir": direction, "A": A, "B": B, "diff": diff, "tf": d["name"]}


def premium_discount(D: Dict[str, Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    leg = active_chart_leg(D["1h"], price) or active_chart_leg(D["15m"], price)
    if not leg:
        return None
    hi = max(leg["A"], leg["B"])
    lo = min(leg["A"], leg["B"])
    if hi <= lo:
        return None
    pos = max(0.0, min(100.0, (price - lo) / (hi - lo) * 100.0))
    zone = "پریمیوم (بالا)" if pos > 50 else "دیسکانت (پایین)"
    return {"pos": pos, "zone": zone, "hi": hi, "lo": lo}


# --------------------------------------------------------------------------
# Real-Time Setup Builder (Entry, SL, TP1/TP2/TP3)
# --------------------------------------------------------------------------
def _structural_targets(
    D: Dict[str, Dict[str, Any]],
    side: str,
    entry: float,
    sl: float,
    leg: Optional[Dict[str, Any]],
) -> List[Tuple[str, float, float]]:
    """Generates clean targets strictly beyond entry in the trade direction,
    combining live swing liquidity pools and Fibonacci levels."""
    risk_pct = abs(sl - entry) / entry * 100.0
    cost = 2.0 * FEE_PCT
    if risk_pct <= 0:
        return []

    def net_rr(t: float) -> float:
        if side == "LONG" and t <= entry:
            return 0.0
        if side == "SHORT" and t >= entry:
            return 0.0
        dist_pct = abs(t - entry) / entry * 100.0
        return (dist_pct - cost) / (risk_pct + cost)

    cands: List[Tuple[str, float]] = []
    if leg:
        A, B, d = leg["A"], leg["B"], leg["diff"]
        if side == "LONG" and leg["dir"] == "UP":
            cands.append(("سقف موج فعال (1.0)", B))
            cands.append(("اکستنشن فیبو 1.272", A + 1.272 * d))
            cands.append(("اکستنشن فیبو 1.618", A + 1.618 * d))
        elif side == "SHORT" and leg["dir"] == "DOWN":
            cands.append(("کف موج فعال (1.0)", B))
            cands.append(("اکستنشن فیبو 1.272", A - 1.272 * d))
            cands.append(("اکستنشن فیبو 1.618", A - 1.618 * d))

    # Add real swing highs/lows from 15m and 1h
    for tf_name, lbl in (("15m", "نقدینگی 15m"), ("1h", "ساختار 1H")):
        d_tf = D[tf_name]
        pts = [p for _, p in (d_tf["highs"] if side == "LONG" else d_tf["lows"])]
        for p in pts:
            if (side == "LONG" and p > entry) or (side == "SHORT" and p < entry):
                cands.append((lbl, p))

    # Sort nearest-first in trade direction
    cands.sort(key=lambda x: abs(x[1] - entry))

    min_step_pct = max(0.35, 0.55 * risk_pct)
    selected: List[Tuple[str, float, float]] = []
    last_dist = 0.0

    for lbl, lvl in cands:
        ratio = net_rr(lvl)
        if ratio < MIN_RR1_NET or ratio > MAX_TP_RR:
            continue
        dist_pct = abs(lvl - entry) / entry * 100.0
        if dist_pct < last_dist + min_step_pct:
            continue
        selected.append((lbl, lvl, ratio))
        last_dist = dist_pct
        if len(selected) == 3:
            break

    return selected


def build_realtime_plan(D: Dict[str, Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    """Constructs a chart-authentic trade plan anchored to the active 15m/1H leg,
    OB/FVG zone, and Liquidity Sweep/MSS."""
    d15, d1, d4 = D["15m"], D["1h"], D["4h"]
    atr15 = d15["atr"]

    leg15 = active_chart_leg(d15, price)
    leg1h = active_chart_leg(d1, price)
    leg = leg15 or leg1h
    if not leg:
        return None

    # Determine bias from active leg + HTF confirmation
    t4, t1, t15 = d4["trend"], d1["trend"], d15["trend"]
    bull_votes = (t4 == "UP") + (t1 == "UP") + (t15 == "UP") + (leg["dir"] == "UP")
    bear_votes = (t4 == "DOWN") + (t1 == "DOWN") + (t15 == "DOWN") + (leg["dir"] == "DOWN")

    if bear_votes > bull_votes and leg["dir"] == "DOWN":
        side = "SHORT"
    elif bull_votes > bear_votes and leg["dir"] == "UP":
        side = "LONG"
    else:
        return None

    pd_ = premium_discount(D, price)
    if side == "SHORT" and pd_ and pd_["pos"] < 40.0:
        return None
    if side == "LONG" and pd_ and pd_["pos"] > 60.0:
        return None

    sweep_obj = None
    mss_obj = None
    for dd in (d15, D["5m"]):
        sw = detect_sweep(dd, side, 20)
        if sw:
            sweep_obj = sw
            m = detect_mss(dd, side, sw)
            if m:
                mss_obj = m
                break

    if not (sweep_obj or mss_obj):
        return None

    A, B, diff = leg["A"], leg["B"], leg["diff"]
    zone = pick_active_zone([d15, D["5m"]], side, price, atr15)

    if side == "SHORT":
        fib_618 = B + 0.618 * diff
        fib_786 = B + 0.786 * diff
        if zone:
            z_mid = (zone["bottom"] + zone["top"]) / 2.0
            # Anchor entry to the active OB/FVG zone, blended with 0.618 if close
            if abs(fib_618 - z_mid) <= 0.7 * atr15:
                entry = (fib_618 + z_mid) / 2.0
                src = "فیبو ۰.۶۱۸ + %s (%s)" % (zone["kind"], zone["tf"])
            else:
                entry = z_mid
                src = "ناحیه عرضه %s (%s)" % (zone["kind"], zone["tf"])
            sl_base = max(zone["top"], fib_786 if abs(fib_786 - entry) <= 1.5 * atr15 else zone["top"])
        else:
            entry = fib_618
            sl_base = fib_786
            src = "فیبو ۰.۶۱۸ موج فعال (%s)" % leg["tf"]

        sl = sl_base + 0.25 * atr15
        if sweep_obj and sweep_obj["extreme"] > entry:
            sl = max(sl, sweep_obj["extreme"] + 0.2 * atr15)
        if price >= sl:
            return None
    else:
        fib_618 = B - 0.618 * diff
        fib_786 = B - 0.786 * diff
        if zone:
            z_mid = (zone["bottom"] + zone["top"]) / 2.0
            if abs(fib_618 - z_mid) <= 0.7 * atr15:
                entry = (fib_618 + z_mid) / 2.0
                src = "فیبو ۰.۶۱۸ + %s (%s)" % (zone["kind"], zone["tf"])
            else:
                entry = z_mid
                src = "ناحیه تقاضا %s (%s)" % (zone["kind"], zone["tf"])
            sl_base = min(zone["bottom"], fib_786 if abs(fib_786 - entry) <= 1.5 * atr15 else zone["bottom"])
        else:
            entry = fib_618
            sl_base = fib_786
            src = "فیبو ۰.۶۱۸ موج فعال (%s)" % leg["tf"]

        sl = sl_base - 0.25 * atr15
        if sweep_obj and sweep_obj["extreme"] < entry:
            sl = min(sl, sweep_obj["extreme"] - 0.2 * atr15)
        if price <= sl:
            return None

    dist_pct = (entry - price) / price * 100.0
    if abs(dist_pct) > MAX_ENTRY_DIST_PCT:
        return None

    risk_pct = abs(sl - entry) / entry * 100.0
    if risk_pct < MIN_STOP_PCT or risk_pct > MAX_STOP_PCT:
        return None

    targets = _structural_targets(D, side, entry, sl, leg)
    if not targets:
        return None

    tp1_lbl, tp1, rr1 = targets[0]
    tp2_lbl, tp2, rr2 = targets[1] if len(targets) > 1 else ("", None, None)
    tp3_lbl, tp3, rr3 = targets[2] if len(targets) > 2 else ("", None, None)

    in_zone = abs(dist_pct) <= 0.15
    if in_zone:
        status = "قیمت در ناحیه ورود؛ آماده واکنش کندلی"
    elif side == "SHORT":
        status = "منتظر پولبک به محدوده ورود (%+.2f%%)" % dist_pct
    else:
        status = "منتظر اصلاح به محدوده ورود (%+.2f%%)" % dist_pct

    confluence = bool(sweep_obj and mss_obj)
    return {
        "side": side,
        "entry": entry,
        "entry_src": src,
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
        "dist_pct": dist_pct,
        "status": status,
        "confluence": confluence,
        "sweep": sweep_obj,
        "mss": mss_obj,
    }


# --------------------------------------------------------------------------
# Reporting & Formatting
# --------------------------------------------------------------------------
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
    p = r.get("plan")
    if not p:
        return None
    side = p["side"]
    entry, sl = p["entry"], p["sl"]
    risk_pct = p["risk_pct"]
    cost = 2.0 * FEE_PCT

    lev = max(1.0, min(MAX_LEVERAGE, TARGET_LEVERAGE))
    notional = CAPITAL_USDT * lev
    actual_risk_usdt = notional * ((risk_pct + cost) / 100.0)
    actual_risk_pct = (actual_risk_usdt / CAPITAL_USDT) * 100.0 if CAPITAL_USDT > 0 else 0.0

    how = "بازار" if abs(p["dist_pct"]) <= 0.15 else "لیمیت"
    badge = "🟢 Sweep + MSS" if p.get("confluence") else ("🟡 Sweep" if p.get("sweep") else "🔵 MSS")

    lines = [
        "<b>%s</b> (قیمت: <code>%s</code>) | <b>%s</b> (%s) | %s"
        % (html.escape(r["symbol"]), fmt_price(r["price"]), side, how, badge),
        "• نقطه ورود: <code>%s</code> (%+.2f%%) — %s"
        % (fmt_price(entry), p["dist_pct"], html.escape(p["entry_src"])),
        "• حد ضرر (SL): <code>%s</code> (%.2f%%)" % (fmt_price(sl), risk_pct),
    ]
    for idx, (tp_key, lbl_key, rr_key) in enumerate(
        [("tp1", "tp1_lbl", "rr1"), ("tp2", "tp2_lbl", "rr2"), ("tp3", "tp3_lbl", "rr3")],
        start=1,
    ):
        val = p.get(tp_key)
        ratio = p.get(rr_key)
        lbl = p.get(lbl_key) or ""
        if val is not None and ratio is not None:
            lines.append(
                "• TP%d [%s]: <code>%s</code> (RR خالص %.2f)"
                % (idx, html.escape(lbl), fmt_price(val), ratio)
            )
    lines.append(
        "• اهرم: <b>%.0fx</b> | حجم پوزیشن: <b>%.2f</b> | ریسک کل: %.2f%% (%.2f واحد)"
        % (lev, notional, actual_risk_pct, actual_risk_usdt)
    )
    lines.append("• وضعیت: %s" % html.escape(p["status"]))
    return lines


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

    bias = "لانگ" if score > 0 else "شورت" if score < 0 else "خنثی"
    strength = "قوی" if abs(score) >= 3 else "ضعیف" if abs(score) >= 1 else "خنثی"
    side = "LONG" if bias == "لانگ" else "SHORT" if bias == "شورت" else None
    sw_obj = (
        (detect_sweep(D["15m"], side, 20) or detect_sweep(D["5m"], side, 20))
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
    plan = r.get("plan")
    if plan:
        entry_txt = "ورود %s | SL %s | TP1 %s" % (
            fmt_price(plan["entry"]), fmt_price(plan["sl"]), fmt_price(plan["tp1"])
        )
    else:
        entry_txt = "بدون ستاپ نزدیک چارت"
    return (
        "<b>%s</b> (%s): <b>%s</b> (%s) | روند: %s | %s | Sweep: %s | %s"
        % (
            html.escape(r["symbol"]),
            fmt_price(price),
            bias,
            strength,
            trend,
            zone,
            sweep_txt,
            html.escape(entry_txt),
        )
    )


def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]]) -> str:
    lev = max(1.0, min(MAX_LEVERAGE, TARGET_LEVERAGE))
    lines = [
        "📊 <b>تحلیل زنده پرایس‌اکشن، نقدینگی و فیبوناچی</b> — %s" % local_now(),
        "اهرم تنظیم‌شده: <b>%.0fx</b> | حداکثر فاصله ورود: %.1f%%" % (lev, MAX_ENTRY_DIST_PCT),
    ]
    setups: List[Tuple[int, List[str]]] = []
    for r in results:
        p = r.get("plan")
        block = full_entry_block_fa(r)
        if p and block:
            rank_val = (2 if p.get("confluence") else 1 if p.get("sweep") else 0) * 100 + int(
                round(p.get("rr1", 0.0) * 10)
            )
            setups.append((rank_val, block))
    setups.sort(key=lambda x: -x[0])

    lines.append("ارزهای بررسی‌شده: %d | ستاپ‌های دقیق و هم‌راستا: %d" % (len(results), len(setups)))
    if setups:
        lines.append("")
        lines.append("🎯 <b>نقاط ورود، حد ضرر و تارگت‌های مشخص</b>")
        for _, block in setups[:6]:
            lines.append("")
            lines.extend(block)
    else:
        lines.append("")
        lines.append("⏸ در این لحظه هیچ ارزی هم‌زمان شرایط شکار نقدینگی + ناحیه ورود نزدیک را ندارد.")

    lines.append("")
    lines.append("🧭 <b>نمای کلی بازار</b>")
    for r in results:
        try:
            lines.append(coin_view_fa(r))
        except Exception:
            lines.append("%s: خطا در تحلیل" % html.escape(r["symbol"]))

    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:120])))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Execution & Telegram
# --------------------------------------------------------------------------
def analyze_symbol(symbol: str) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    price = fetch_price(symbol) or C["5m"].c[-1]
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    last_close = D["5m"]["close"]
    if abs(price - last_close) / last_close > 0.02:
        price = last_close
    plan = build_realtime_plan(D, price)
    return {"symbol": symbol, "price": price, "D": D, "plan": plan}


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
    ap = argparse.ArgumentParser(description="Real-time Price Action & Fibonacci Scanner")
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
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
            errors.append((sym, "جفت‌ارز در LBank پیدا نشد"))
        symbols = [x for x in symbols if x.lower() in pairs]

    for sym in symbols:
        try:
            results.append(analyze_symbol(sym))
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
