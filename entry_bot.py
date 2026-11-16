#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
entry_bot.py - Fibonacci-only entry points (impulse 0.618 entry, 0.786 stop, 1.272 target). Planning only.
Impulse source order: 1H -> 4H -> 15m (so every symbol gets a plan when a clear swing exists).
Each report line includes: trend 4H/1H/15m, 5m sweep, 5m MSS, nearest OB/FVG zone.
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
ARROW = {"UP": "↑", "DOWN": "↓", "RANGE": "↔"}
ss.TF_MAP.setdefault("1m", ("minute1", 60))
CONFIRM_VOL = ss._env_float("CONFIRM_VOL", 1.2)
CONFIRM_BODY = ss._env_float("CONFIRM_BODY", 0.5)


def recent_impulse(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    n = len(d["cs"])
    lo_bar = n - RECENT_BARS
    hs = [h for h in d["highs"] if h[0] >= lo_bar]
    ls = [l for l in d["lows"] if l[0] >= lo_bar]
    if not hs or not ls:
        return None
    hi_i, hi_v = hs[-1]
    lo_i, lo_v = ls[-1]
    if lo_i > hi_i:
        A, B, direction = hi_v, lo_v, "DOWN"
    else:
        A, B, direction = lo_v, hi_v, "UP"
    diff = abs(A - B)
    if diff < MIN_IMPULSE_ATR * d["atr"]:
        return None
    return {"dir": direction, "A": A, "B": B, "diff": diff, "tf": d["name"]}


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
            status = "قیمت از حد ضرر گذشته؛ منقضی"
        elif price >= entry:
            status = "در ناحیه‌ی ورود؛ منتظر کندل رد (بسته‌شدن ۱۵m زیر ورود)"
        else:
            status = "منتظر رسیدن قیمت به ورود (بالا)"
    else:
        side = "LONG"
        entry = B - 0.618 * d
        sl = B - 0.786 * d - SL_BUF_ATR * atr15
        tp1, tp2 = B, A + 1.272 * d
        if price < sl:
            status = "قیمت از حد ضرر گذشته؛ منقضی"
        elif price <= entry:
            status = "در ناحیه‌ی ورود؛ منتظر کندل رد (بسته‌شدن ۱۵m بالای ورود)"
        else:
            status = "منتظر رسیدن قیمت به ورود (پایین)"
    rr1 = net_rr(entry, tp1, sl)
    rr2 = net_rr(entry, tp2, sl)
    risk_pct = abs(sl - entry) / entry * 100.0
    lev = min(ss.MAX_LEVERAGE, ss.RISK_PCT / (risk_pct + 2.0 * ss.FEE_PCT))
    quality = "معتبر" if rr1 >= 1.0 else "ضعیف (RR TP1 کمتر از ۱)"
    dist_pct = (entry - price) / price * 100.0
    expiry = datetime.now(timezone.utc) + timedelta(hours=ss._env_float("TZ_OFFSET_HOURS", 3.5) + EXPIRY_H)
    return {"side": side, "A": A, "B": B, "tf": imp["tf"], "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2, "risk_pct": risk_pct,
            "leverage": lev, "status": status, "quality": quality, "dist_pct": dist_pct,
            "expiry": expiry.strftime("%Y-%m-%d %H:%M") + " (محلی)"}


def price_action(D: Dict[str, Dict[str, Any]], side: str, price: float) -> str:
    """Short, concrete read: trends, 5m sweep/MSS for this side, nearest OB/FVG zone."""
    trend = " ".join(ARROW[D[t]["trend"]] for t in ("4h", "1h", "15m"))
    s5 = ss.detect_sweep(D["5m"], side, 24)
    if s5:
        sweep_txt = "Sweep 5m (%s)" % ("نقدینگی فروش" if side == "LONG" else "نقدینگی خرید")
        m5 = ss.detect_mss(D["5m"], side, s5)
        mss_txt = "MSS 5m: %s" % ("بله" if m5 else "خیر")
    else:
        sweep_txt, mss_txt = "Sweep 5m: خیر", "MSS 5m: —"
    z = ss.pick_zone([D["15m"], D["5m"]], side, price, D["15m"]["atr"])
    zone_txt = ("ناحیه %s در %s: %s–%s" % (z["kind"], z["tf"], ss.fmt_price(z["bottom"]), ss.fmt_price(z["top"]))
                if z else "ناحیه‌ی OB/FVG نزدیک: ندارد")
    return "روند 4H/1H/15m: %s | %s | %s | %s" % (trend, sweep_txt, mss_txt, zone_txt)


def confirm_1m(plan: Dict[str, Any], c1: Any) -> Dict[str, Any]:
    """1m entry trigger on closed candles: price must touch the entry, close back on the
    trade side, with body >= CONFIRM_BODY x avg body and volume >= CONFIRM_VOL x avg volume."""
    if c1 is None or len(c1.c) < 25:
        return {"ok": False, "text": "کندل ۱m کافی نیست (تأیید ورود انجام نشد)"}
    o, h, l, c, v = c1.o[-1], c1.h[-1], c1.l[-1], c1.c[-1], c1.v[-1]
    entry = plan["entry"]
    avg_body = sum(abs(c1.c[i] - c1.o[i]) for i in range(-21, -1)) / 20.0
    avg_vol = sum(c1.v[-21:-1]) / 20.0
    body_ok = avg_body > 0 and abs(c - o) >= CONFIRM_BODY * avg_body
    vol_ok = avg_vol > 0 and v >= CONFIRM_VOL * avg_vol
    if plan["side"] == "SHORT":
        touched = h >= entry
        rejected = c < o and c < entry
    else:
        touched = l <= entry
        rejected = c > o and c > entry
    if touched and rejected and body_ok and vol_ok:
        return {"ok": True, "text": "تأیید ۱m: کندل رد بسته‌شده با بدنه و حجم کافی — ورود فعال"}
    if not touched:
        return {"ok": False, "text": "منتظر رسیدن قیمت به ورود %s (کندل ۱m هنوز ورود را لمس نکرده)" % ss.fmt_price(entry)}
    return {"ok": False, "text": "در ناحیه‌ی ورود؛ منتظر کندل ۱m رد با بدنه و حجم کافی (بدنه %s، حجم %s)" % (
        "کافی" if body_ok else "کم", "کافی" if vol_ok else "کم")}


def analyze(symbol: str) -> Dict[str, Any]:
    C = {tf: ss.fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m", "5m")}
    D = {tf: ss.build_tf(C[tf], tf) for tf in ("4h", "1h", "15m", "5m")}
    price = ss.fetch_price(symbol) or C["15m"].c[-1]
    imp = recent_impulse(D["1h"]) or recent_impulse(D["4h"]) or recent_impulse(D["15m"])
    if imp is None:
        return {"symbol": symbol, "price": price, "plan": None,
                "reason": "هیچ ایمپالس واضحی در ۴H/۱H/۱۵m پیدا نشد (حرکت آخر کوچک است)"}
    plan = make_plan(imp, D, price)
    try:
        c1 = ss.fetch_klines(symbol, "1m", size=120)
    except ss.ScanError:
        c1 = None
    trig = confirm_1m(plan, c1)
    plan["confirmed"] = trig["ok"]
    plan["trigger"] = trig["text"]
    return {"symbol": symbol, "price": price, "plan": plan,
            "analysis": price_action(D, plan["side"], price), "reason": ""}


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
        "ایمپالس %s: %s→%s | وضعیت: %s" % (p["tf"], fmt(p["A"]), fmt(p["B"]), p["status"]),
        "تریگر: %s" % p["trigger"],
        "اهرم تا %.2fx | انقضا: %s" % (p["leverage"], p["expiry"]),
    ]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Entry points with price-action context (no orders)")
    ap.add_argument("--symbols", default=ss.DEFAULT_SYMBOLS)
    ap.add_argument("--send", action="store_true", help="send the report to Telegram")
    args = ap.parse_args(argv)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = ss.fetch_pairs()
    if pairs:
        syms = [s for s in syms if s.lower() in pairs]
    out: List[str] = ["🎯 <b>نقاط ورود فیبوناچی + پرایس‌اکشن</b> — %s" % ss.local_now(), ""]
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
