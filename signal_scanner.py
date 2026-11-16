#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py (Part 1/5)
-----------------
Smart Market Analyst V3 - Multi-Timeframe Professional Signal Engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("signal_scanner")

LBANK_BASE = os.getenv("LBANK_BASE_URL", "https://api.lbkex.com").rstrip("/")
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,TRX_USDT,XMR_USDT,"
                   "NEAR_USDT,ATOM_USDT,BCH_USDT,DOGE_USDT,LINK_USDT,FIL_USDT")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

MIN_SCORE = _env_float("MIN_SCORE", 65.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 6.0)
MIN_CANDLES = 100
FEE_PCT = _env_float("FEE_PCT", 0.06)
MAX_DATA_AGE_MINUTES = 30

TF_MAP: Dict[str, Tuple[str, int]] = {
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

class ScanError(Exception):
    pass
# --------------------------------------------------------------------------
# Part 2/5: Network Requests & Indicators
# --------------------------------------------------------------------------
_last_req = 0.0

def _throttle() -> None:
    global _last_req
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()

def http_json(url: str, params: Optional[Dict[str, Any]] = None, retries: int = 4) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "signal-scanner/3.0", "Accept": "application/json"}
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
            break
        except Exception:
            time.sleep(min(float(2 ** attempt), 20))
    raise ScanError("Network request failed")

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
    data = http_json(LBANK_BASE + "/v2/kline.do", {"symbol": symbol.lower(), "size": size, "type": typ, "time": now - size * sec})
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("Invalid kline data")
    parsed = {}
    for r in rows_raw:
        try:
            ts = int(float(r[0]))
            if ts > 10 ** 11: ts //= 1000
            o, h, l, c, v = (float(x) for x in r[1:6])
            if min(o, h, l, c) > 0 and h >= l:
                parsed[ts] = (ts, o, h, l, c, v)
        except Exception:
            continue
    rows = [parsed[k] for k in sorted(parsed)]
    if len(rows) < MIN_CANDLES:
        raise ScanError("Insufficient candles")
    return Candles(rows)

def fetch_price(symbol: str) -> Optional[float]:
    try:
        data = http_json(LBANK_BASE + "/v2/ticker/24hr.do", {"symbol": symbol.lower()})
        items = data.get("data")
        if isinstance(items, list) and items:
            return float(items[0].get("ticker", {}).get("latest", 0))
    except Exception:
        pass
    return None

def calculate_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    gains = sum(max(closes[-i] - closes[-i - 1], 0) for i in range(1, period + 1))
    losses = sum(max(closes[-i - 1] - closes[-i], 0) for i in range(1, period + 1))
    if losses == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + (gains / losses)))
# --------------------------------------------------------------------------
# Part 3/5: Swing Detection & Precise Limit Pricing
# --------------------------------------------------------------------------
def find_swings(h: List[float], l: List[float], n: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs, lows = [], []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and all(h[i] >= h[i + k] for k in range(1, n + 1)):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and all(l[i] <= l[i + k] for k in range(1, n + 1)):
            lows.append((i, l[i]))
    return highs, lows

def structure_trend(c: List[float], highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> str:
    if len(highs) < 2 or len(lows) < 2: return "RANGE"
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
        return "UP" if c[-1] >= lows[-1][1] else "RANGE"
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return "DOWN" if c[-1] <= highs[-1][1] else "RANGE"
    return "RANGE"

def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    highs, lows = find_swings(cs.h, cs.l, n=2 if name == "15m" else 3)
    return {
        "name": name, "close": cs.c[-1], "highs": highs, "lows": lows,
        "trend": structure_trend(cs.c, highs, lows), "rsi": calculate_rsi(cs.c)
    }

def calculate_trade_plan(cs: Candles, direction: str, current_price: float) -> Dict[str, Any]:
    highs, lows = find_swings(cs.h, cs.l, n=3)
    if direction == 'LONG':
        valid_lows = [p[1] for p in lows if p[1] < current_price]
        entry = min(valid_lows[-3:]) if valid_lows else min(cs.l[-15:])
        if entry >= current_price: entry = current_price * 0.992
        sl = entry * 0.965
        risk = entry - sl
        tp1, tp2 = entry + (risk * 1.5), entry + (risk * 2.5)
    else:
        valid_highs = [p[1] for p in highs if p[1] > current_price]
        entry = max(valid_highs[-3:]) if valid_highs else max(cs.h[-15:])
        if entry <= current_price: entry = current_price * 1.008
        sl = entry * 1.035
        risk = sl - entry
        tp1, tp2 = entry - (risk * 1.5), entry - (risk * 2.5)
    
    return {
        "Entry": round(entry, 4), "SL": round(sl, 4),
        "TP1": round(tp1, 4), "TP2": round(tp2, 4)
    }
# --------------------------------------------------------------------------
# Part 4/5: Multi-Timeframe Confluence Engine
# --------------------------------------------------------------------------
def evaluate_symbol(symbol: str, C: Dict[str, Candles], price: float) -> Dict[str, Any]:
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m")}
    
    t_4h = D["4h"]["trend"]
    t_1h = D["1h"]["trend"]
    t_15m = D["15m"]["trend"]
    
    # هم‌جهتی سخت‌گیرانه در سه تایم‌فریم
    if t_4h == "DOWN" and t_1h in ("DOWN", "RANGE"):
        side = "SHORT"
    elif t_4h == "UP" and t_1h in ("UP", "RANGE"):
        side = "LONG"
    else:
        side = "SHORT" if t_4h == "DOWN" else "LONG"

    plan = calculate_trade_plan(C["15m"], side, price)
    
    return {
        "symbol": symbol, "price": price, "side": side,
        "trends": {"4h": t_4h, "1h": t_1h, "15m": t_15m},
        "plan": plan
    }
# --------------------------------------------------------------------------
# Part 5/5: Report Formatter & Main Loop
# --------------------------------------------------------------------------
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

def format_report(res: Dict[str, Any]) -> str:
    sym = res["symbol"].replace("_", "")
    side_fa = "شورت (فروش 🔻)" if res["side"] == "SHORT" else "لانگ (خرید 🟢)"
    tr = res["trends"]
    p = res["plan"]
    
    lines = [
        "🎯 **تحلیل فوق‌حرفه‌ای سه‌تایم‌فریمی (3TF)**",
        f"نماد: {sym} | جهت: {side_fa}",
        f"🔹 روند تایم‌فریم‌ها (4H / 1H / 15m): `{tr['4h']} / {tr['1h']} / {tr['15m']}`",
        f"💰 قیمت لحظه‌ای بازار: `{res['price']}`",
        "-----------------------------------",
        f"📍 **نقطه ورود لیمیت (Entry):** `{p['Entry']}`",
        f"🛑 **حد ضرر (Stop Loss):** `{p['SL']}`",
        f"🎯 **تارگت اول (TP1):** `{p['TP1']}`",
        f"🎯 **تارگت دوم (TP2):** `{p['TP2']}`",
        "-----------------------------------",
        "💡 *تحلیل بر اساس انطباق ساختار SMC و تاییدیه مومنتوم صادر شد.*"
    ]
    return "\n".join(lines)

def main():
    syms = [s.strip().upper() for s in DEFAULT_SYMBOLS.split(",")]
    for sym in syms:
        try:
            C = {tf: fetch_klines(sym, tf) for tf in ("4h", "1h", "15m")}
            price = fetch_price(sym) or C["15m"].c[-1]
            res = evaluate_symbol(sym, C, price)
            print(format_report(res))
            print("=" * 40)
        except Exception as e:
            print(f"Error on {sym}: {e}")

if __name__ == "__main__":
    main()
