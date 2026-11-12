#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py (Part 1/5)
-----------------
Smart Market Analyst V3 - SMC + LIT + RSI/MACD Momentum + Adaptive Risk & 3-Tier Outputs.
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
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,"
                   "ADA_USDT,NEAR_USDT,BCH_USDT,DOGE_USDT,ATOM_USDT,XRP_USDT")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

MIN_SCORE = _env_float("MIN_SCORE", 65.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 4.0)  # انعطاف بیشتر برای حد ضرر با تنظیم اهرم
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

class ScanError(Exception):
    """Recoverable error for a single symbol / request."""
# --------------------------------------------------------------------------
# Part 2/5: Network Requests, Candle Parsing & Momentum (RSI, MACD)
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

def calculate_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_macd(closes: List[float]) -> Tuple[float, float]:
    if len(closes) < 26:
        return 0.0, 0.0
    ema12 = sum(closes[-12:]) / 12.0
    ema26 = sum(closes[-26:]) / 26.0
    macd_val = ema12 - ema26
    signal_val = macd_val * 0.9 # ساده‌سازی تخمین سیگنال لاین
    return macd_val, signal_val
# --------------------------------------------------------------------------
# Part 3/5: Structure, Adaptive Risk/Leverage & Fibonacci Confluence
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

def calculate_fib_levels(swing_high: float, swing_low: float, direction: str) -> Dict[str, float]:
    diff = abs(swing_high - swing_low)
    if diff <= 0:
        return {}
    if direction == 'LONG':
        return {"fib_500": swing_high - (diff * 0.5), "fib_618": swing_high - (diff * 0.618), "fib_786": swing_high - (diff * 0.786)}
    else:
        return {"fib_500": swing_low + (diff * 0.5), "fib_618": swing_low + (diff * 0.618), "fib_786": swing_low + (diff * 0.786)}

def check_fib_confluence(entry: float, swings_h: List[Tuple[int, float]], swings_l: List[Tuple[int, float]], side: str, atr_val: float) -> Tuple[bool, str]:
    if not swings_h or not swings_l:
        return False, "بدون سویگ معتبر"
    recent_high = max([p[1] for p in swings_h[-5:]]) if swings_h else entry * 1.02
    recent_low = min([p[1] for p in swings_l[-5:]]) if swings_l else entry * 0.98
    fibs = calculate_fib_levels(recent_high, recent_low, side)
    if not fibs:
        return False, "محاسبه فیبو مقدور نیست"
    tolerance = 0.6 * atr_val
    if min(fibs["fib_618"], fibs["fib_786"]) - tolerance <= entry <= max(fibs["fib_618"], fibs["fib_786"]) + tolerance:
        return True, "انطباق عالی با محدوده طلایی فیبو (0.618 - 0.786)"
    elif abs(entry - fibs["fib_500"]) <= tolerance:
        return True, "انطباق با سطح اصلاحی 0.5 فیبو"
    return False, "خارج از محدوده طلایی فیبو"

def structure_dir(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> int:
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl: return 1
    if lh and ll: return -1
    return 0

def structure_trend(c: List[float], highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> str:
    if not highs or not lows: return "RANGE"
    st = structure_dir(highs, lows)
    last = c[-1]
    if st == 1: return "RANGE" if last < lows[-1][1] else "UP"
    if st == -1: return "RANGE" if last > highs[-1][1] else "DOWN"
    if last > highs[-1][1]: return "UP"
    if last < lows[-1][1]: return "DOWN"
    return "RANGE"

def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    c, h, l = cs.c, cs.h, cs.l
    at = atr(h, l, c)
    highs, lows = find_swings(h, l, 2 if name == "4h" else 3)
    st = structure_dir(highs, lows)
    trend = structure_trend(c, highs, lows)
    return {
        "name": name, "cs": cs, "close": c[-1], "atr": max(at[-1], c[-1] * 1e-6),
        "vol_ratio": volume_ratio(cs.v), "highs": highs, "lows": lows, "struct": st,
        "trend": trend, "rsi": calculate_rsi(c), "macd": calculate_macd(c)
    }

def validate_and_correct_trade_plan(direction: str, entry: float, sl: float, tp1: float, tp2: float, swings_h: List[Tuple[int, float]], swings_l: List[Tuple[int, float]]) -> Dict[str, Any]:
    errors = []
    risk = abs(entry - sl)
    if risk <= 0: risk = entry * 0.005
    risk_pct = (risk / entry) * 100.0
    
    # مدیریت تطبیقی اهرم به جای رد خودکار بالای ۳٪
    if risk_pct > MAX_STOP_PCT:
        errors.append("حد ضرر ساختاری بیش از حد مجاز است (%.2f%%)" % risk_pct)

    min_reward1 = risk * 1.5
    min_reward2 = risk * 2.5
    if direction == 'LONG':
        if (tp1 - entry) < min_reward1: tp1 = entry + min_reward1
        if (tp2 - entry) < min_reward2: tp2 = entry + min_reward2
    else:
        if (entry - tp1) < min_reward1: tp1 = entry - min_reward1
        if (entry - tp2) < min_reward2: tp2 = entry - min_reward2

    total_fee_rate = 2.0 * FEE_PCT / 100.0
    net_rr1 = round((abs(tp1 - entry) - (entry * total_fee_rate)) / risk, 2) if risk > 0 else 0.0
    net_rr2 = round((abs(tp2 - entry) - (entry * total_fee_rate)) / risk, 2) if risk > 0 else 0.0
    valid = net_rr2 >= 1.45 and not errors

    return {
        "SL": round(sl, 4), "TP1": round(tp1, 4), "TP2": round(tp2, 4),
        "Net_RR1": net_rr1, "Net_RR2": net_rr2, "Valid": valid, "Entry": round(entry, 4),
        "Validator_Errors": errors, "Risk_Pct": round(risk_pct, 2)
    }

def calculate_levels_and_targets(cs: Candles, direction='LONG', entry_price=0, swings_h=None, swings_l=None):
    lookback = 60
    highs_raw = cs.h[-lookback:]
    lows_raw = cs.l[-lookback:]
    if swings_h is None or swings_l is None:
        swings_h, swings_l = find_swings(cs.h, cs.l, n=3)
    structural_low = min(lows_raw) if lows_raw else entry_price * 0.99
    structural_high = max(highs_raw) if highs_raw else entry_price * 1.01
    
    if direction == 'LONG':
        sl = structural_low * 0.992
        risk = entry_price - sl
        tp1, tp2 = entry_price + (risk * 1.6), entry_price + (risk * 2.8)
    else:
        sl = structural_high * 1.008
        risk = sl - entry_price
        tp1, tp2 = entry_price - (risk * 1.6), entry_price - (risk * 2.8)
    return validate_and_correct_trade_plan(direction, entry_price, sl, tp1, tp2, swings_h, swings_l)
# --------------------------------------------------------------------------
# Part 4/5: LIT, Momentum Confluence (RSI/MACD) & Scoring Engine
# --------------------------------------------------------------------------
def detect_sweep(d: Dict[str, Any], side: str, lookback: int) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    a = d["atr"]
    long = side == "LONG"
    pts = [p for p in (d["lows"] if long else d["highs"]) if p[0] >= n - 150]
    for j in range(n - 1, max(n - 1 - lookback, 2), -1):
        for si, lvl in reversed(pts):
            if si >= j - 1: continue
            if long:
                ok = cs.l[j] < lvl - 0.03 * a and cs.c[j] > lvl and all(x >= lvl for x in cs.c[si + 1:j])
            else:
                ok = cs.h[j] > lvl + 0.03 * a and cs.c[j] < lvl and all(x <= lvl for x in cs.c[si + 1:j])
            if ok:
                return {"idx": j, "level": lvl, "tf": d["name"]}
    return None

def detect_mss(d: Dict[str, Any], side: str, sweep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    a = d["atr"]
    j = sweep["idx"]
    end = min(len(cs), j + MSS_MAX_DELAY + 1)
    if side == "LONG":
        prior = [p for p in d["highs"] if p[0] < j]
        if not prior: return None
        lvl = prior[-1][1]
        for k in range(j, end):
            if cs.c[k] > lvl and (cs.c[k] - cs.o[k]) >= MSS_MIN_BODY * a:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    else:
        prior = [p for p in d["lows"] if p[0] < j]
        if not prior: return None
        lvl = prior[-1][1]
        for k in range(j, end):
            if cs.c[k] < lvl and (cs.o[k] - cs.c[k]) >= MSS_MIN_BODY * a:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    return None

def pick_zone(ds: List[Dict[str, Any]], side: str, price: float, atr15: float) -> Optional[Dict[str, Any]]:
    return {"bottom": price * 0.995, "top": price * 1.005, "kind": "Zone", "tf": "15m"}

def evaluate_side(side: str, D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    long = side == "LONG"
    want = "UP" if long else "DOWN"
    d4, d1, d15 = D["4h"], D["1h"], D["15m"]
    notes, blockers, soft, parts = [], [], [], {}

    sw15 = detect_sweep(d15, side, 12)
    sw5 = detect_sweep(D["5m"], side, 24)
    chosen = None
    if sw15 and detect_mss(d15, side, sw15):
        chosen = (d15, sw15)
    elif sw5 and detect_mss(D["5m"], side, sw5):
        chosen = (D["5m"], sw5)
    structure_ok = bool(chosen)

    # بررسی هم‌جهتی مومنتوم (RSI و MACD) برای امتیاز مثبت
    rsi_val = d15["rsi"]
    macd_val, macd_sig = d15["macd"]
    momentum_aligned = (long and rsi_val > 50 and macd_val > macd_sig) or (not long and rsi_val < 50 and macd_val < macd_sig)
    if momentum_aligned:
        parts["momentum"] = 15.0
        notes.append("⚡ هم‌جهتی مثبت RSI و MACD")
    else:
        parts["momentum"] = 5.0

    # بررسی حجم مناسب
    v_ratio = d15["vol_ratio"]
    if v_ratio >= 1.2:
        parts["volume"] = 15.0
        notes.append("📈 حجم معاملات ایده‌آل و صعودی")
    elif v_ratio >= 0.8:
        parts["volume"] = 10.0
    else:
        soft.append("حجم معاملات ضعیف است")
        parts["volume"] = 3.0

    continuation_override = (not long and d1["trend"] == "DOWN" and v_ratio >= 1.2 and structure_ok)

    if side == "LONG" and d4["trend"] == "DOWN":
        blockers.append("روند 4H نزولی است")
    elif side == "SHORT" and d4["trend"] == "UP" and not continuation_override:
        blockers.append("روند 4H صعودی است")

    s4 = 12 if d4["trend"] == want else 5 if continuation_override else 0
    s1 = 13 if d1["trend"] == want else 6 if continuation_override else 0
    parts["htf"] = s4 + s1
    parts["zone"] = 10.0
    parts["room"] = 5.0
    parts["rr"] = 10.0

    swings_h, swings_l = find_swings(d15["cs"].h, d15["cs"].l, n=3)
    fib_matched, fib_note = check_fib_confluence(price, swings_h, swings_l, side, d15["atr"])
    if fib_matched or continuation_override:
        parts["fibonacci"] = 10.0
        notes.append("📐 " + (fib_note if fib_matched else "پولبک مقاومتی تایید شد"))
    else:
        parts["fibonacci"] = 2.0

    validated_plan = calculate_levels_and_targets(d15["cs"], direction=side, entry_price=price, swings_h=swings_h, swings_l=swings_l)

    trade = None
    if validated_plan["Validator_Errors"]:
        blockers.extend(validated_plan["Validator_Errors"])
    elif not validated_plan["Valid"]:
        blockers.append("RR خالص کمتر از حد نصاب است")
    else:
        cost = 2.0 * FEE_PCT
        risk_pct = validated_plan["Risk_Pct"]
        notional_mult = (RISK_PCT / 100.0) / ((risk_pct + cost) / 100.0)
        lev = min(MAX_LEVERAGE, notional_mult)
        trade = {
            "entry": validated_plan["Entry"], "sl": validated_plan["SL"],
            "tp1": validated_plan["TP1"], "tp2": validated_plan["TP2"],
            "rr1": validated_plan["Net_RR1"], "rr2": validated_plan["Net_RR2"],
            "stop_pct": risk_pct, "notional_x": lev
        }

    base_score = int(round(sum(parts.values())))
    score = max(0, min(100, base_score))
    if continuation_override: score = max(score, 75)

    return {
        "side": side, "score": score, "parts": parts, "notes": notes,
        "blockers": blockers, "soft_blockers": soft, "trade": trade,
        "structure_ok": structure_ok or continuation_override, "debug_plan": validated_plan
    }
# --------------------------------------------------------------------------
# Part 5/5: 3-Tier Output Logic & Main Execution
# --------------------------------------------------------------------------
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

def classify_decision(score: float, structure_ok: bool, blockers: List[str]) -> str:
    """
    قوانین خروجی ۳گانه:
    - امتیاز ≥ 80 و ساختار کامل: ENTER (ورود با تأیید ساختار و مدیریت ریسک)
    - امتیاز 65 تا 79: CONDITIONAL (نقاط مشخص + شرط فعال‌شدن)
    - امتیاز < 65 یا ساختار نامعتبر: NO_ENTRY (عدم ورود)
    """
    if score >= 80 and structure_ok and not blockers:
        return "ENTER"
    elif score >= 65 and score < 80:
        return "CONDITIONAL"
    return "NO_ENTRY"

def analyze_candles(symbol: str, C: Dict[str, Candles], price: float, min_score: float) -> Dict[str, Any]:
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    
    best_sc = max(scenarios.values(), key=lambda x: x["score"])
    decision = classify_decision(best_sc["score"], best_sc["structure_ok"], best_sc["blockers"])
    
    signal = best_sc if decision == "ENTER" else None
    watch = best_sc if decision == "CONDITIONAL" else None

    return {
        "symbol": symbol, "price": price, "D": D, "scenarios": scenarios,
        "signal": signal, "watch": watch, "best_sc": best_sc, "decision": decision
    }

def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    price = fetch_price(symbol) or C["5m"].c[-1]
    return analyze_candles(symbol, C, price, min_score)

def fmt_price(p: float) -> str:
    if p >= 1000: return "%.2f" % p
    if p >= 1: return "%.4f" % p
    if p >= 0.01: return "%.5f" % p
    return "%.8f" % p

def format_brief(res: Dict[str, Any]) -> List[str]:
    sc = res["best_sc"]
    D = res["D"]
    dbg = sc.get("debug_plan", {})
    dec = res["decision"]
    notes = [n for n in sc["notes"] if n.startswith("📐") or "هم‌جهتی" in n or "حجم" in n]
    
    lines = [
        "<b>%s</b> | سناریو: %s | امتیاز: %d | تصمیم: <b>%s</b>" % (html.escape(res["symbol"]), sc["side"], sc["score"], dec),
        "روند 4H/1H/15m: %s %s %s" % (TREND_ARROW[D["4h"]["trend"]], TREND_ARROW[D["1h"]["trend"]], TREND_ARROW[D["15m"]["trend"]]),
    ]
    if notes:
        lines.append("تحلیل تکنیکال: " + html.escape(" | ".join(notes)))
    if dbg and sc.get("trade"):
        lines.append("🎯 [طرح معاملاتی] Entry: %s | SL: %s (%.2f%%)" % (fmt_price(dbg.get("Entry", 0)), fmt_price(dbg.get("SL", 0)), dbg.get("Risk_Pct", 0)))
        lines.append("▫️ TP1: %s (RR1: <b>%.2f</b>) | TP2: %s (RR2: <b>%.2f</b>)" % (fmt_price(dbg.get("TP1", 0)), dbg.get("Net_RR1", 0), fmt_price(dbg.get("TP2", 0)), dbg.get("Net_RR2", 0)))
    if sc["blockers"] or sc["soft_blockers"]:
        lines.append("❌ علت مسدودسازی: " + html.escape(" | ".join(sc["blockers"] + sc["soft_blockers"][:1])))
    return lines

def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]]) -> str:
    now = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M (به وقت ایران)")
    sigs = [r for r in results if r["decision"] == "ENTER"]
    lines = ["📊 <b>گزارش Smart Market Analyst V3 + 3-Tier System</b> — %s" % now]
    lines.append("🎯 سیگنال‌های قطعی ورود (ENTER): %d" % len(sigs))
    
    top = sorted(results, key=lambda r: -r["best_sc"]["score"])
    shown = [r for r in top if r["best_sc"]["score"] >= WATCH_MIN][:8]
    
    if shown:
        lines.append("")
        lines.append("🧭 <b>واچ‌لیست ارزیابی‌شده (امتیاز ≥ %d)</b>" % WATCH_MIN)
        for r in shown:
            lines.append("")
            lines.extend(format_brief(r))
            
    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:150])))
    lines.append("")
    lines.append("سیستم ۳گانه خروجی (Enter / Conditional / No Entry) فعال")
    return "\n".join(lines)

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results, errors = [], []
    pairs = fetch_pairs()
    if pairs:
        symbols = [x for x in symbols if x.lower() in pairs]
        
    for sym in symbols:
        try:
            results.append(analyze_symbol(symbol=sym, min_score=args.min_score))
        except ScanError as e:
            errors.append((sym, str(e)))
        except Exception as e:
            errors.append((sym, "خطای سیستمی: %s" % str(e)))

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
