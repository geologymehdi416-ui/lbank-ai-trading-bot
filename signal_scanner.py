#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py (Part 1/5)
-----------------
Smart Market Analyst V3 - Strict Trend Gate, Precise Swings & Uncorrelated Assets.
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
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,TRX_USDT,XMR_USDT,"
                   "NEAR_USDT,ATOM_USDT,BCH_USDT,DOGE_USDT,LINK_USDT,FIL_USDT")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

MIN_SCORE = _env_float("MIN_SCORE", 65.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 6.0)
MIN_CANDLES = 100
FEE_PCT = _env_float("FEE_PCT", 0.06)
WATCH_MIN = 60
STRUCT_CAP = 70
MSS_MAX_DELAY = 8
MSS_MIN_BODY = 0.8
MAX_DATA_AGE_MINUTES = 30

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
# Part 2/5: Network Requests, Candle Freshness & Momentum Indicators
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
        if diff >= 0: gains += diff
        else: losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def calculate_macd(closes: List[float]) -> Tuple[float, float]:
    if len(closes) < 26: return 0.0, 0.0
    ema12 = sum(closes[-12:]) / 12.0
    ema26 = sum(closes[-26:]) / 26.0
    return (ema12 - ema26), (ema12 - ema26) * 0.9
# --------------------------------------------------------------------------
# Part 3/5: Structure, Precise Swing Limit Entry & Fee-Adjusted RR
# --------------------------------------------------------------------------
def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[float]:
    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out, a = [], 0.0
    for i, tr in enumerate(trs):
        if i < n - 1: out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            a = sum(trs[:n]) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out

def volume_ratio(v: List[float]) -> float:
    if len(v) < 24: return 1.0
    recent = sum(v[-3:]) / 3.0
    base = sum(v[-23:-3]) / 20.0
    return recent / base if base > 0 else 1.0

def find_swings(h: List[float], l: List[float], n: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs, lows = [], []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and all(h[i] >= h[i + k] for k in range(1, n + 1)):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and all(l[i] <= l[i + k] for k in range(1, n + 1)):
            lows.append((i, l[i]))
    return highs, lows

def calculate_fib_levels(swing_high: float, swing_low: float, direction: str) -> Dict[str, float]:
    diff = abs(swing_high - swing_low)
    if diff <= 0: return {}
    if direction == 'LONG':
        return {"fib_500": swing_high - (diff * 0.5), "fib_618": swing_high - (diff * 0.618), "fib_786": swing_high - (diff * 0.786)}
    else:
        return {"fib_500": swing_low + (diff * 0.5), "fib_618": swing_low + (diff * 0.618), "fib_786": swing_low + (diff * 0.786)}

def check_fib_confluence(entry: float, swings_h: List[Tuple[int, float]], swings_l: List[Tuple[int, float]], side: str, atr_val: float) -> Tuple[bool, str]:
    if not swings_h or not swings_l: return False, "بدون سویگ معتبر"
    fibs = calculate_fib_levels(max([p[1] for p in swings_h[-5:]]), min([p[1] for p in swings_l[-5:]]), side)
    if not fibs: return False, "محاسبه فیبو مقدور نیست"
    tol = 0.6 * atr_val
    if min(fibs["fib_618"], fibs["fib_786"]) - tol <= entry <= max(fibs["fib_618"], fibs["fib_786"]) + tol:
        return True, "انطباق عالی با محدوده طلایی فیبو (0.618 - 0.786)"
    elif abs(entry - fibs["fib_500"]) <= tol:
        return True, "انطباق با سطح اصلاحی 0.5 فیبو"
    return False, "خارج از محدوده طلایی فیبو"

def structure_dir(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> int:
    if len(highs) < 2 or len(lows) < 2: return 0
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: return 1
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: return -1
    return 0

def structure_trend(c: List[float], highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> str:
    if not highs or not lows: return "RANGE"
    st = structure_dir(highs, lows)
    last = c[-1]
    if st == 1: return "RANGE" if last < lows[-1][1] else "UP"
    if st == -1: return "RANGE" if last > highs[-1][1] else "DOWN"
    return "UP" if last > highs[-1][1] else ("DOWN" if last < lows[-1][1] else "RANGE")

def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    c, h, l = cs.c, cs.h, cs.l
    at = atr(h, l, c)
    highs, lows = find_swings(h, l, 2 if name == "4h" else 3)
    return {
        "name": name, "cs": cs, "close": c[-1], "atr": max(at[-1], c[-1] * 1e-6),
        "vol_ratio": volume_ratio(cs.v), "highs": highs, "lows": lows,
        "trend": structure_trend(c, highs, lows), "rsi": calculate_rsi(c), "macd": calculate_macd(c)
    }

def validate_and_correct_trade_plan(direction: str, entry: float, sl: float, tp1: float, tp2: float) -> Dict[str, Any]:
    errors = []
    risk = abs(entry - sl)
    if risk <= 0: risk = entry * 0.005
    risk_pct = (risk / entry) * 100.0
    
    if risk_pct > MAX_STOP_PCT:
        errors.append("حد ضرر ساختاری بیش از حد مجاز است (%.2f%%)" % risk_pct)

    min_r1, min_r2 = risk * 1.5, risk * 2.5
    if direction == 'LONG':
        if (tp1 - entry) < min_r1: tp1 = entry + min_r1
        if (tp2 - entry) < min_r2: tp2 = entry + min_r2
    else:
        if (entry - tp1) < min_r1: tp1 = entry - min_r1
        if (entry - tp2) < min_r2: tp2 = entry - min_r2

    total_fee_rate = 2.0 * FEE_PCT / 100.0
    net_reward1 = abs(tp1 - entry) - (entry * total_fee_rate)
    net_reward2 = abs(tp2 - entry) - (entry * total_fee_rate)
    net_rr1 = round(net_reward1 / risk, 2) if risk > 0 else 0.0
    net_rr2 = round(net_reward2 / risk, 2) if risk > 0 else 0.0
    valid = net_rr2 >= 1.45 and not errors

    return {
        "SL": round(sl, 4), "TP1": round(tp1, 4), "TP2": round(tp2, 4),
        "Net_RR1": net_rr1, "Net_RR2": net_rr2, "Valid": valid, "Entry": round(entry, 4),
        "Validator_Errors": errors, "Risk_Pct": round(risk_pct, 2)
    }

def calculate_levels_and_targets(cs: Candles, direction='LONG', entry_price=0):
    highs, lows = find_swings(cs.h, cs.l, n=3)
    current_close = cs.c[-1]
    
    if direction == 'LONG':
        valid_lows = [p[1] for p in lows if p[1] < current_close]
        entry = max(valid_lows[-3:]) if valid_lows else min(cs.l[-20:])
        sl = entry * 0.96
        risk = entry - sl
        tp1 = entry + (risk * 1.6)
        tp2 = entry + (risk * 2.8)
    else:
        valid_highs = [p[1] for p in highs if p[1] > current_close]
        entry = min(valid_highs[-3:]) if valid_highs else max(cs.h[-20:])
        sl = entry * 1.04
        risk = sl - entry
        tp1 = entry - (risk * 1.6)
        tp2 = entry - (risk * 2.8)
        
    return validate_and_correct_trade_plan(direction, entry, sl, tp1, tp2)
# --------------------------------------------------------------------------
# Part 4/5: Scenario Evaluation & Strict Trend Gate Veto Engine
# --------------------------------------------------------------------------
def detect_sweep(d: Dict[str, Any], side: str, lookback: int) -> Optional[Dict[str, Any]]:
    cs, n, a = d["cs"], len(d["cs"]), d["atr"]
    long = side == "LONG"
    pts = [p for p in (d["lows"] if long else d["highs"]) if p[0] >= n - 150]
    for j in range(n - 1, max(n - 1 - lookback, 2), -1):
        for si, lvl in reversed(pts):
            if si >= j - 1: continue
            ok = (cs.l[j] < lvl - 0.03 * a and cs.c[j] > lvl) if long else (cs.h[j] > lvl + 0.03 * a and cs.c[j] < lvl)
            if ok: return {"idx": j, "level": lvl, "tf": d["name"]}
    return None

def detect_mss(d: Dict[str, Any], side: str, sweep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cs, a, j = d["cs"], d["atr"], sweep["idx"]
    end = min(len(cs), j + MSS_MAX_DELAY + 1)
    prior = [p for p in (d["highs"] if side == "LONG" else d["lows"]) if p[0] < j]
    if not prior: return None
    lvl = prior[-1][1]
    for k in range(j, end):
        if side == "LONG" and cs.c[k] > lvl and (cs.c[k] - cs.o[k]) >= MSS_MIN_BODY * a:
            return {"idx": k, "level": lvl, "tf": d["name"]}
        elif side == "SHORT" and cs.c[k] < lvl and (cs.o[k] - cs.c[k]) >= MSS_MIN_BODY * a:
            return {"idx": k, "level": lvl, "tf": d["name"]}
    return None

def evaluate_side(side: str, D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    long = side == "LONG"
    d4, d1, d15 = D["4h"], D["1h"], D["15m"]
    notes, blockers, soft, parts = [], [], [], {}

    # 🛑 فیلتر آهنین جهت روند (Trend Gate Veto): جلوگیری قطعی از لانگ در روند نزولی یا شورت در صعودی
    if long and d4["trend"] == "DOWN":
        blockers.append("روند 4H نزولی است (وتو شدن قطعی پوزیشن لانگ)")
    elif not long and d4["trend"] == "UP":
        blockers.append("روند 4H صعودی است (وتو شدن قطعی پوزیشن شورت)")

    sw15 = detect_sweep(d15, side, 12)
    sw5 = detect_sweep(D["5m"], side, 24)
    structure_ok = bool((sw15 and detect_mss(d15, side, sw15)) or (sw5 and detect_mss(D["5m"], side, sw5)))

    rsi, (macd_v, macd_s) = d15["rsi"], d15["macd"]
    momentum_ok = (long and rsi > 50 and macd_v > macd_s) or (not long and rsi < 50 and macd_v < macd_s)
    parts["momentum"] = 15.0 if momentum_ok else 5.0
    if momentum_ok: notes.append("⚡ هم‌جهتی مثبت مومنتوم (RSI و MACD)")
    else: notes.append("⚠️ مومنتوم ضعیف/خلاف جهت")

    v_ratio = d15["vol_ratio"]
    parts["volume"] = 15.0 if v_ratio >= 1.2 else (10.0 if v_ratio >= 0.8 else 3.0)
    if v_ratio >= 1.2: notes.append("📈 تایید حجم معاملات ایده‌آل")
    else: soft.append("حجم معاملات ضعیف است")

    parts["htf"] = (12 if d4["trend"] == ("UP" if long else "DOWN") else 2)
    parts["zone"], parts["room"], parts["rr"] = 10.0, 5.0, 10.0

    swings_h, swings_l = find_swings(d15["cs"].h, d15["cs"].l, n=3)
    fib_ok, fib_note = check_fib_confluence(price, swings_h, swings_l, side, d15["atr"])
    parts["fibonacci"] = 10.0 if fib_ok else 2.0
    if fib_ok: notes.append("📐 " + fib_note)

    validated_plan = calculate_levels_and_targets(d15["cs"], direction=side, entry_price=price)
    if validated_plan["Validator_Errors"]:
        blockers.extend(validated_plan["Validator_Errors"])
    elif not validated_plan["Valid"]:
        blockers.append("RR خالص کمتر از حد نصاب است")

    score = max(0, min(100, int(round(sum(parts.values())))))
    if blockers:
        score = min(score, 45) # افت امتیاز شدید در صورت وجود وتوی روند یا خطای ساختاری

    return {
        "side": side, "score": score, "parts": parts, "notes": notes,
        "blockers": blockers, "soft_blockers": soft, "trade": validated_plan if not validated_plan["Validator_Errors"] else None,
        "structure_ok": structure_ok, "debug_plan": validated_plan
    }
# --------------------------------------------------------------------------
# Part 5/5: Clean Telegram Format, Analysis & Safe Reporting
# --------------------------------------------------------------------------
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

def classify_decision(score: float, structure_ok: bool, blockers: List[str], min_score: float) -> str:
    has_critical = any("وتو" in b or "روند" in b or "حد ضرر" in b for b in blockers)
    if score >= 80 and structure_ok and not has_critical and not blockers:
        return "ENTER"
    elif score >= min_score and not has_critical:
        return "CONDITIONAL"
    return "NO_ENTRY"

def analyze_candles(symbol: str, C: Dict[str, Candles], price: float, min_score: float) -> Dict[str, Any]:
    D = {tf: build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    
    latest_ts = max(C["5m"].t[-1], C["15m"].t[-1])
    age_min = (time.time() - latest_ts) / 60.0
    is_stale = age_min > MAX_DATA_AGE_MINUTES

    scenarios = {s: evaluate_side(s, D, price) for s in ("LONG", "SHORT")}
    best_sc = max(scenarios.values(), key=lambda x: x["score"])
    
    if is_stale:
        best_sc["blockers"].append("داده‌های بازار قدیمی هستند (تاخیر > ۳۰ دقیقه)")
        decision = "NO_ENTRY"
    else:
        decision = classify_decision(best_sc["score"], best_sc["structure_ok"], best_sc["blockers"], min_score)

    return {
        "symbol": symbol, "price": price, "D": D, "scenarios": scenarios,
        "best_sc": best_sc, "decision": decision, "data_age_min": age_min
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
    dbg = sc.get("debug_plan", {})
    symbol_clean = res["symbol"].replace("_", "")
    side_fa = "شورت" if sc["side"] == "SHORT" else "لانگ"
    
    lines = [
        "⏳ سفارش لیمیت در انتظار رسیدن قیمت",
        "",
        "شناسه معامله: TR-%s" % datetime.now(IRAN_TZ).strftime("%H%M%S"),
        "نماد: %s" % symbol_clean,
        "جهت: %s" % side_fa,
        "ورود لیمیت: %s" % fmt_price(dbg.get("Entry", 0)),
        "اهرم: 5x",
        "اهداف: 1️⃣ %s" % fmt_price(dbg.get("TP1", 0)),
        "2️⃣ %s" % fmt_price(dbg.get("TP2", 0)),
        "حد ضرر: %s" % fmt_price(dbg.get("SL", 0)),
        "",
        "📊 **تحلیل و دلایل فنی ربات:**",
        "• امتیاز کیفیت ستاپ: %d/100" % sc["score"],
    ]
    
    for note in sc.get("notes", []):
        lines.append("• %s" % note)
        
    if sc["blockers"]:
        lines.append("❌ علت رد: " + sc["blockers"][0])
        
    return lines

def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]], min_score: float) -> str:
    now = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M (به وقت ایران)")
    shown = [r for r in results if r["best_sc"]["score"] >= WATCH_MIN][:8]
    lines = []
    
    if not shown and not errors:
        lines.append("⏳ اسکن بازار — %s" % now)
        lines.append("در این چرخه، فرصت معاملاتی باکیفیتی با امتیاز ≥ %d یافت نشد." % WATCH_MIN)
    else:
        for r in shown:
            lines.extend(format_brief(r))
            lines.append("-" * 25)
                
        for sym, err in errors:
            lines.append("❗ %s: %s" % (sym, err[:100]))
            
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
            res = analyze_symbol(symbol=sym, min_score=args.min_score)
            results.append(res)
            
            if res["decision"] in ("ENTER", "CONDITIONAL") and res["best_sc"].get("trade"):
                log_entry = {
                    "timestamp": datetime.now(IRAN_TZ).isoformat(),
                    "symbol": res["symbol"],
                    "side": res["best_sc"]["side"],
                    "decision": res["decision"],
                    "score": res["best_sc"]["score"],
                    "plan": res["best_sc"]["trade"]
                }
                os.makedirs("logs", exist_ok=True)
                with open("logs/signal_history.jsonl", "a", encoding="utf-8") as lf:
                    lf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except ScanError as e:
            errors.append((sym, str(e)))
        except Exception as e:
            errors.append((sym, "خطای سیستمی: %s" % str(e)))

    report_text = format_report(results, errors, args.min_score)
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