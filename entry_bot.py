#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
entry_bot.py - concrete entry points from the most recent 1H Fibonacci impulse.
Planning only, never places orders.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import signal_scanner as ss

RECENT_BARS = int(ss._env_float("RECENT_BARS", 120))
MIN_IMPULSE_ATR = ss._env_float("MIN_IMPULSE_ATR", 1.5)
SL_BUF_ATR = 0.2
EXPIRY_H = ss._env_float("ENTRY_EXPIRY_H", 6.0)


def recent_impulse(d1: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    n = len(d1["cs"])
    lo_bar = n - RECENT_BARS
    hs = [h for h in d1["highs"] if h[0] >= lo_bar]
    ls = [l for l in d1["lows"] if l[0] >= lo_bar]
    if not hs or not ls:
        return None
    hi_i, hi_v = hs[-1]
    lo_i, lo_v = ls[-1]
    if lo_i > hi_i:
        A, B, direction = hi_v, lo_v, "DOWN"
    else:
        A, B, direction = lo_v, hi_v, "UP"
    diff = abs(A - B)
    if diff < MIN_IMPULSE_ATR * d1["atr"]:
        return None
    return {"dir": direction, "A": A, "B": B, "diff": diff}


def net_rr(entry: float, target: float, sl: float) -> float:
    cost = 2.0 * ss.FEE_PCT
    risk_pct = abs(sl - entry) / entry * 100.0
    return (abs(target - entry) / entry * 100.0 - cost) / (risk_pct + cost)


def make_plan(imp: Dict[str, Any], D: Dict[str, Dict[str, Any]], price: float) -> Dict[str, Any]:
    A, B, d = imp["A"], imp["B"], imp["diff"]
    atr15 = D["15m"]["atr"]
    if imp["dir"] == "DOWN":
        side = "SHORT"
        entry = B + 0.618 * d
        sl = B + 0.786 * d + SL_BUF_ATR * atr15
        tp1, tp2 = B, A - 1.272 * d
        if price > sl:
            status = "قیمت از حد ضرر گذشته؛ این نقطه منقضی است"
        elif price >= entry:
            status = "قیمت در ناحیه‌ی ورود؛ منتظر کندل رد (بسته‌شدن ۱۵m زیر ورود)"
        else:
            status = "منتظر رسیدن قیمت به ورود (بالا)"
    else:
        side = "LONG"
        entry = B - 0.618 * d
        sl = B - 0.786 * d - SL_BUF_ATR * atr15
        tp1, tp2 = B, A + 1.272 * d
        if price < sl:
            status = "قیمت از حد ضرر گذشته؛ این نقطه منقضی است"
        elif price <= entry:
            status = "قیمت در ناحیه‌ی ورود؛ منتظر کندل رد (بسته‌شدن ۱۵m بالای ورود)"
        else:
            status = "منتظر رسیدن قیمت به ورود (پایین)"
    rr1 = net_rr(entry, tp1, sl)
    rr2 = net_rr(entry, tp2, sl)
    risk_pct = abs(sl - entry) / entry * 100.0
    lev = min(ss.MAX_LEVERAGE, ss.RISK_PCT / (risk_pct + 2.0 * ss.FEE_PCT))
    quality = "معتبر" if rr1 >= 1.0 else "ضعیف (RR TP1 کمتر از ۱)"
    dist_pct = (entry - price) / price * 100.0
    expiry = datetime.now(timezone.utc) + timedelta(hours=ss._env_float("TZ_OFFSET_HOURS", 3.5) + EXPIRY_H)
    return {"side": side, "A": A, "B": B, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
            "rr1": rr1, "rr2": rr2, "risk_pct": risk_pct, "leverage": lev, "status": status,
            "quality": quality, "dist_pct": dist_pct,
            "expiry": expiry.strftime("%Y-%m-%d %H:%M") + " (محلی)"}


def analyze(symbol: str) -> Optional[Dict[str, Any]]:
    C = {tf: ss.fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m")}
    D = {tf: ss.build_tf(C[tf], tf) for tf in ("4h", "1h", "15m")}
    price = ss.fetch_price(symbol) or C["15m"].c[-1]
    imp = recent_impulse(D["1h"])
    if imp is None:
        return {"symbol": symbol, "price": price, "plan": None,
                "reason": "ایمپالس اخیر ۱H مشخص نیست"}
    plan = make_plan(imp, D, price)
    imp4 = recent_impulse(D["4h"])
    if imp4 is None:
        ctx = "۴H: ایمپالس اخیر مشخص نیست"
    elif imp4["dir"] == imp["dir"]:
        ctx = "۴H هم‌راستا"
    else:
        ctx = "۴H خلاف (احتیاط)"
    return {"symbol": symbol, "price": price, "plan": plan, "context": ctx, "reason": ""}


def fmt(x: float) -> str:
    return ss.fmt_price(x)


def format_one(r: Dict[str, Any]) -> List[str]:
    if r["plan"] is None:
        return ["<b>%s</b> | %s | — %s" % (r["symbol"], fmt(r["price"]), r["reason"])]
    p = r["plan"]
    return [
        "<b>%s</b> | %s | <b>%s</b> | %s" % (r["symbol"], fmt(r["price"]), p["side"], p["quality"]),
        "ورود: <b>%s</b> (فاصله %+.2f%%) | حد ضرر: %s (%.2f%%)" % (
            fmt(p["entry"]), p["dist_pct"], fmt(p["sl"]), p["risk_pct"]),
        "TP1: %s (RR خالص %.2f) | TP2: %s (RR خالص %.2f)" % (
            fmt(p["tp1"]), p["rr1"], fmt(p["tp2"]), p["rr2"]),
        "وضعیت: %s | ایمپالس ۱H: %s→%s | %s" % (p["status"], fmt(p["A"]), fmt(p["B"]), r["context"]),
        "اهرم تا %.2fx | انقضا: %s" % (p["leverage"], p["expiry"]),
    ]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Concrete Fibonacci entry points (no orders)")
    ap.add_argument("--symbols", default=ss.DEFAULT_SYMBOLS)
    ap.add_argument("--send", action="store_true", help="send the report to Telegram")
    args = ap.parse_args(argv)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = ss.fetch_pairs()
    if pairs:
        syms = [s for s in syms if s.lower() in pairs]
    out: List[str] = ["🎯 <b>نقاط ورود فیبوناچی</b> — %s" % ss.local_now(), ""]
    for sym in syms:
        try:
            out.extend(format_one(analyze(sym)))
            out.append("")
        except ss.ScanError as e:
            out.append("❗ %s: %s" % (sym, e))
            out.append("")
    text = "\n".join(out)
    print(ss.to_plain(text))
    with open("entry_plan.txt", "w", encoding="utf-8") as f:
        f.write(ss.to_plain(text) + "\n")
    if args.send:
        ss.send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
