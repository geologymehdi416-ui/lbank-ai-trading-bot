#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
levels_bot.py - support / resistance map with liquidity-grab (sweep) detection.
Analysis only, no orders. Uses signal_scanner.py for candles and swing detection.

For each symbol:
  1. Collect swing highs and lows from 4H, 1H and 15m.
  2. Cluster nearby swings into one level (equal highs/lows = liquidity pools).
     Strength = how many swings (touches) fall into that level, weighted by timeframe.
  3. Above price -> resistance (R1, R2); below price -> support (S1, S2).
  4. Liquidity grab: on the last 20 closed 15m candles, a wick beyond a level that closed back
     inside it. Reported as "sweep" of that level.
Usage:
  python levels_bot.py --symbols BTC_USDT,ETH_USDT,SOL_USDT
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import signal_scanner as ss

TF_WEIGHT = {"4h": 3, "1h": 2, "15m": 1}
CLUSTER_ATR = 0.15      # swings closer than this x ATR(1H) are the same level
SWEEP_ATR = 0.03        # wick must exceed the level by this x ATR(15m)


def collect(D: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    pts = []
    for tf in ("4h", "1h", "15m"):
        d = D[tf]
        w = TF_WEIGHT[tf]
        for _, v in d["highs"]:
            pts.append({"p": v, "w": w, "kind": "H", "tf": tf})
        for _, v in d["lows"]:
            pts.append({"p": v, "w": w, "kind": "L", "tf": tf})
    return sorted(pts, key=lambda x: x["p"])


def cluster(pts: List[Dict[str, Any]], tol: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for x in pts:
        if out and abs(x["p"] - out[-1]["p"]) <= tol:
            c = out[-1]
            c["w"] += x["w"]
            c["n"] += 1
            c["p"] = (c["p"] * (c["n"] - 1) + x["p"]) / c["n"]
            c["tfs"].add(x["tf"])
        else:
            out.append({"p": x["p"], "w": x["w"], "n": 1, "tfs": {x["tf"]}})
    return out


def sweeps(c15: ss.Candles, level: float, atr15: float, above: bool) -> Optional[int]:
    """Index (candles ago) of the last liquidity grab of this level, or None."""
    n = len(c15)
    for k in range(n - 1, max(n - 21, 0), -1):
        if above:
            if c15.h[k] > level + SWEEP_ATR * atr15 and c15.c[k] < level:
                return n - 1 - k
        else:
            if c15.l[k] < level - SWEEP_ATR * atr15 and c15.c[k] > level:
                return n - 1 - k
    return None


def analyze(symbol: str) -> Dict[str, Any]:
    C = {tf: ss.fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m")}
    D = {tf: ss.build_tf(C[tf], tf) for tf in ("4h", "1h", "15m")}
    price = ss.fetch_price(symbol) or C["15m"].c[-1]
    tol = CLUSTER_ATR * D["1h"]["atr"]
    lv = cluster(collect(D), tol)
    res = [x for x in lv if x["p"] > price]
    sup = [x for x in lv if x["p"] < price]
    res.sort(key=lambda x: x["p"])
    sup.sort(key=lambda x: -x["p"])
    best_r = sorted(res[:6], key=lambda x: -x["w"])[:2]
    best_s = sorted(sup[:6], key=lambda x: -x["w"])[:2]
    atr15 = D["15m"]["atr"]
    def enrich(x: Dict[str, Any], above: bool) -> Dict[str, Any]:
        return {"p": x["p"], "dist": (x["p"] - price) / price * 100.0, "touches": x["n"],
                "tfs": sorted(x["tfs"]), "sweep_ago": sweeps(C["15m"], x["p"], atr15, above)}
    return {"symbol": symbol, "price": price,
            "resistance": [enrich(x, True) for x in sorted(best_r, key=lambda x: x["p"])],
            "support": [enrich(x, False) for x in sorted(best_s, key=lambda x: -x["p"])],
            "trend": " ".join(D[t]["trend"] for t in ("4h", "1h", "15m"))}


def fmt(p: float) -> str:
    return ss.fmt_price(p)


def render(r: Dict[str, Any]) -> List[str]:
    lines = ["<b>%s</b> | قیمت %s | روند 4H/1H/15m: %s" % (r["symbol"], fmt(r["price"]), r["trend"])]
    for x in reversed(r["resistance"]):
        s = " | شکار نقدینگی %d کندل پیش" % x["sweep_ago"] if x["sweep_ago"] is not None else ""
        lines.append("  مقاومت %s (%+.2f%%) | تماس %d | %s%s" % (fmt(x["p"]), x["dist"], x["touches"],
                                                         "/".join(x["tfs"]), s))
    for x in r["support"]:
        s = " | شکار نقدینگی %d کندل پیش" % x["sweep_ago"] if x["sweep_ago"] is not None else ""
        lines.append("  حمایت %s (%+.2f%%) | تماس %d | %s%s" % (fmt(x["p"]), x["dist"], x["touches"],
                                                       "/".join(x["tfs"]), s))
    if not r["resistance"] and not r["support"]:
        lines.append("  سطحی پیدا نشد")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Support/resistance with liquidity-grab detection")
    ap.add_argument("--symbols", default=ss.DEFAULT_SYMBOLS)
    args = ap.parse_args(argv)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = ss.fetch_pairs()
    if pairs:
        syms = [s for s in syms if s.lower() in pairs]
    out = ["🧱 <b>مقاومت و حمایت + شکار نقدینگی</b> — %s" % ss.local_now(), ""]
    for sym in syms:
        try:
            out.extend(render(analyze(sym)))
            out.append("")
        except ss.ScanError as e:
            out.append("❗ %s: %s" % (sym, e))
    text = "\n".join(out)
    print(ss.to_plain(text))
    with open("levels_report.txt", "w", encoding="utf-8") as f:
        f.write(ss.to_plain(text) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
