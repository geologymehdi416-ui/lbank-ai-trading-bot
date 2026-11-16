#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fib_analyst.py - Fibonacci + price-action planner. Planning only, never places orders.

Logic (per symbol):
  1. Find the last completed impulse on 1H (fallback 4H) from confirmed swings.
     Impulse DOWN (high -> low): look for a SHORT retrace up to 0.618-0.786.
     Impulse UP   (low -> high): look for a LONG retrace down to 0.618-0.786.
     So direction follows the market context, not only the 4H trend.
  2. Entry  = 0.618 retrace of the impulse (limit).
     Stop   = beyond the 0.786 level + small ATR(15m) buffer.
     TP1    = impulse extreme (B) - structural.
     TP2    = 1.272 extension of the impulse.
  3. State from live 15m data: WAITING / IN_ZONE / CONFIRMED (rejection candle) / INVALID / EXPIRED.
  4. Each target must have positive net RR after round-trip fee; TP1 net RR < 1 => no entry.
Usage:
  python fib_analyst.py
  python fib_analyst.py --symbols BTC_USDT,ETH_USDT
  python fib_analyst.py --send
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import signal_scanner as ss

FIB_EXPIRY_H = ss._env_float("FIB_EXPIRY_H", 6.0)
MIN_IMPULSE_ATR = ss._env_float("MIN_IMPULSE_ATR", 2.0)
SL_BUF_ATR = 0.2
STATE_FA = {
    "WAITING": "منتظر پولبک",
    "IN_ZONE": "در ناحیه‌ی فیبو: منتظر کندل رد",
    "CONFIRMED": "تأیید شد (کندل رد در ناحیه)",
    "INVALID": "طرح باطل",
    "EXPIRED": "منقضی (تارگت اول لمس شده)",
}


def impulse(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Last completed impulse from confirmed swings. None if no clear impulse."""
    hs, ls = d["highs"], d["lows"]
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


def build_plan(imp: Dict[str, Any], D: Dict[str, Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    A, B, d = imp["A"], imp["B"], imp["diff"]
    atr15 = D["15m"]["atr"]
    c15 = D["15m"]["cs"]
    if imp["dir"] == "DOWN":
        side = "SHORT"
        z_a, z_b = B + 0.618 * d, B + 0.786 * d
        entry = z_a
        sl = z_b + SL_BUF_ATR * atr15
        tp1, tp2 = B, A - 1.272 * d
    else:
        side = "LONG"
        z_a, z_b = B - 0.618 * d, B - 0.786 * d
        entry = z_a
        sl = z_b - SL_BUF_ATR * atr15
        tp1, tp2 = B, A + 1.272 * d
    zone_lo, zone_hi = min(z_a, z_b), max(z_a, z_b)
    if side == "SHORT":
        beyond_sl = price > sl
        deep = price > zone_hi
        waiting = price < zone_lo
        tp_hit = price <= tp1
    else:
        beyond_sl = price < sl
        deep = price < zone_lo
        waiting = price > zone_hi
        tp_hit = price >= tp1

    last_bear = c15.c[-1] < c15.o[-1]
    last_bull = c15.c[-1] > c15.o[-1]
    if tp_hit:
        state = "EXPIRED"
    elif beyond_sl or deep:
        state = "INVALID"
    elif waiting:
        state = "WAITING"
    else:
        rejection = (last_bear and c15.c[-1] < entry) if side == "SHORT" else (last_bull and c15.c[-1] > entry)
        state = "CONFIRMED" if rejection else "IN_ZONE"

    rr1 = net_rr(entry, tp1, sl)
    rr2 = net_rr(entry, tp2, sl)
    risk_pct = abs(sl - entry) / entry * 100.0
    lev = min(ss.MAX_LEVERAGE, ss.RISK_PCT / (risk_pct + 2.0 * ss.FEE_PCT))
    if state in ("INVALID", "EXPIRED"):
        decision = "NO ENTRY"
    elif rr1 < 1.0:
        decision = "NO ENTRY (RR TP1 کم)"
    elif state == "CONFIRMED":
        decision = "ENTER"
    else:
        decision = "PREPARE"
    expiry = (datetime.now(timezone.utc) + timedelta(hours=ss._env_float("TZ_OFFSET_HOURS", 3.5) + FIB_EXPIRY_H))
    return {
        "side": side, "impulse_tf": imp["tf"], "A": A, "B": B, "diff": d,
        "zone": (zone_lo, zone_hi), "entry": entry, "sl": sl,
        "tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2, "risk_pct": risk_pct,
        "leverage": lev, "state": state, "decision": decision,
        "expiry": expiry.strftime("%Y-%m-%d %H:%M") + " (محلی)",
    }


def analyze(symbol: str) -> Optional[Dict[str, Any]]:
    C = {tf: ss.fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m")}
    D = {tf: ss.build_tf(C[tf], tf) for tf in ("4h", "1h", "15m")}
    price = ss.fetch_price(symbol) or C["15m"].c[-1]
    imp = impulse(D["1h"]) or impulse(D["4h"])
    if imp is None:
        return {"symbol": symbol, "price": price, "plan": None, "reason": "impulse مشخص نیست"}
    plan = build_plan(imp, D, price)
    imp4 = impulse(D["4h"])
    if imp4 is None:
        ctx = "ساختار ۴H مشخص نیست"
    elif imp4["dir"] == imp["dir"]:
        ctx = "هم‌راستا با ایمپالس ۴H"
    else:
        ctx = "خلاف ایمپالس ۴H (احتیاط: ضدروند بزرگ‌تر)"
    return {"symbol": symbol, "price": price, "plan": plan, "context": ctx, "reason": ""}


def fmt_p(x: float) -> str:
    return ss.fmt_price(x)


def format_one(r: Dict[str, Any]) -> List[str]:
    lines = ["<b>%s</b> | قیمت %s" % (r["symbol"], fmt_p(r["price"]))]
    p = r["plan"]
    if p is None:
        lines.append("بدون طرح: %s" % r["reason"])
        return lines
    lines.append("جهت: <b>%s</b> | تصمیم: <b>%s</b> | وضعیت: %s" % (
        p["side"], p["decision"], STATE_FA[p["state"]]))
    lines.append("ایمپالس %s: A=%s → B=%s | %s" % (p["impulse_tf"], fmt_p(p["A"]), fmt_p(p["B"]), r["context"]))
    lines.append("ناحیه‌ی فیبو ۰٫۶۱۸–۰٫۷۸۶: %s – %s" % (fmt_p(p["zone"][0]), fmt_p(p["zone"][1])))
    lines.append("ورود (۰٫۶۱۸): %s | حد ضرر: %s (%.2f%%)" % (fmt_p(p["entry"]), fmt_p(p["sl"]), p["risk_pct"]))
    lines.append("TP1: %s (RR خالص %.2f) | TP2: %s (RR خالص %.2f)" % (
        fmt_p(p["tp1"]), p["rr1"], fmt_p(p["tp2"]), p["rr2"]))
    lines.append("اهرم تا %.2fx | انقضا: %s" % (p["leverage"], p["expiry"]))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fibonacci + price-action planner (no orders)")
    ap.add_argument("--symbols", default=ss.DEFAULT_SYMBOLS)
    ap.add_argument("--send", action="store_true", help="send the report to Telegram")
    args = ap.parse_args(argv)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = ss.fetch_pairs()
    if pairs:
        syms = [s for s in syms if s.lower() in pairs]
    out: List[str] = ["📐 <b>تحلیل فیبوناچی و پرایس‌اکشن</b> — %s" % ss.local_now(), ""]
    for sym in syms:
        try:
            r = analyze(sym)
            out.extend(format_one(r))
            out.append("")
        except ss.ScanError as e:
            out.append("❗ %s: %s" % (sym, e))
            out.append("")
    text = "\n".join(out)
    print(ss.to_plain(text))
    with open("fib_report.txt", "w", encoding="utf-8") as f:
        f.write(ss.to_plain(text) + "\n")
    if args.send:
        ss.send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())