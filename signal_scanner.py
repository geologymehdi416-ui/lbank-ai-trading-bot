#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LBank Futures snapshot-only scanner. No candles, no orders, no other exchanges."""
import csv, json, math, os, re
from collections import defaultdict, Counter
from datetime import datetime, timezone

D = "data"
WATCHLIST = [x.strip().upper() for x in os.getenv(
    "LBANK_WATCHLIST",
    "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,AVAX,LINK,LTC,HYPE,SUI,AAVE,UNI,NEAR,ICP,RENDER,ONDO,PYTH,JUP"
).split(",") if x.strip()][:20]
QUOTE = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()
MAX_SNAPSHOT_AGE = float(os.getenv("MAX_SNAPSHOT_AGE_MIN", "15"))
MAX_BOOK_AGE = float(os.getenv("MAX_BOOK_AGE_MIN", "15"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
MIN_DEPTH = float(os.getenv("MIN_DEPTH_QUOTE", "1000"))
MIN_SCORE = int(os.getenv("MIN_SCORE_B", "60"))

def now():
    return datetime.now(timezone.utc)

def dt(v):
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x.replace(tzinfo=timezone.utc) if x.tzinfo is None else x.astimezone(timezone.utc)
    except Exception:
        return None

def n(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None

def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())

def base(row):
    b = str(row.get("baseCurrency") or "").upper().strip()
    if b:
        return b
    s, q = norm(row.get("symbol")), norm(QUOTE)
    return s[:-len(q)] if q and s.endswith(q) else s

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    x = json.loads(line)
                    if isinstance(x, dict): out.append(x)
                except Exception: pass
    except Exception: pass
    return out

def latest(rows):
    out = {}
    for x in rows:
        s, t = norm(x.get("symbol")), dt(x.get("timestamp"))
        if s and t and (s not in out or t > dt(out[s].get("timestamp"))):
            out[s] = x
    return out

def age(x, t):
    z = dt(x.get("timestamp")) if x else None
    return (t-z).total_seconds()/60 if z else None

def tick_round(v, tick):
    return round(v/tick)*tick if tick and tick > 0 else v

def analyze(symbol, asset, snap, book, contract, t):
    reasons = []
    if not snap:
        return {"symbol": symbol, "base_asset": asset, "status": "INSUFFICIENT_DATA",
                "side": None, "score": 0, "price": None,
                "analysis": {"reasons": ["برای این نماد snapshot قیمت دریافت نشده"], "conditional_plan": {}}}
    px = n(snap.get("lastPrice"))
    sa, ba = age(snap, t), age(book, t)
    if px is None or px <= 0 or sa is None or sa < -1 or sa > MAX_SNAPSHOT_AGE:
        reasons.append("snapshot قیمت نامعتبر یا قدیمی است")
    if not book or ba is None or ba < -1 or ba > MAX_BOOK_AGE:
        reasons.append("دفتر سفارش موجود نیست یا قدیمی است")

    bid, ask = n((book or {}).get("bestBid")), n((book or {}).get("bestAsk"))
    spread = n((book or {}).get("spreadPct"))
    bd = n((book or {}).get("bidDepthQuote")) or 0
    ad = n((book or {}).get("askDepthQuote")) or 0
    depth = bd + ad
    valid_book = bool(bid and ask and bid > 0 and ask > bid and spread is not None)
    imb = (bd-ad)/depth if depth > 0 else None
    if not valid_book:
        reasons.append("ساختار Bid/Ask یا spread دفتر سفارش نامعتبر است")

    hi, lo = n(snap.get("highPrice24h")), n(snap.get("lowPrice24h"))
    range_ok = bool(hi and lo and hi > lo > 0)
    if not range_ok:
        reasons.append("High/Low معتبر ۲۴ساعته در snapshot موجود نیست")

    r24 = None
    op = n(snap.get("openPrice24h"))
    if px and op and op > 0: r24 = (px/op-1)*100

    candidates = []
    for side in ("LONG", "SHORT"):
        sign = 1 if side == "LONG" else -1
        score, parts = 0, {}
        def award(k, ok, pts):
            nonlocal score
            parts[k] = {"passed": bool(ok), "points": pts if ok else 0}
            if ok: score += pts

        mid = (hi+lo)/2 if range_ok else None
        award("24h_momentum", r24 is not None and sign*r24 > 0, 25)
        award("24h_range_position", range_ok and px is not None and
              ((side == "LONG" and px >= mid) or (side == "SHORT" and px <= mid)), 20)
        award("orderbook_imbalance", imb is not None and sign*imb >= 0.08, 30)
        award("spread", valid_book and spread <= MAX_SPREAD, 15)
        award("depth", depth >= MIN_DEPTH, 10)

        entry = sl = tp1 = tp2 = trigger = rr = None
        if range_ok and px:
            tk = n(contract.get("priceTick"))
            if side == "LONG" and px < hi and px < mid:
                trigger, sl = tick_round(hi*1.001, tk), tick_round(mid, tk)
                entry = trigger
                risk = entry-sl
                if risk > 0: tp1, tp2 = entry+1.5*risk, entry+2*risk
            elif side == "SHORT" and px > lo and px > mid:
                trigger, sl = tick_round(lo*0.999, tk), tick_round(mid, tk)
                entry = trigger
                risk = sl-entry
                if risk > 0: tp1, tp2 = entry-1.5*risk, entry-2*risk
            if entry is not None and tp1 is not None:
                rr = abs(tp1-entry)/abs(entry-sl)
            else:
                entry = sl = tp1 = tp2 = trigger = None

        hard_ok = (not reasons and valid_book and spread <= MAX_SPREAD and depth >= MIN_DEPTH
                   and entry is not None and rr is not None and rr >= 1.5)
        if reasons:
            status, why = "INSUFFICIENT_DATA", list(dict.fromkeys(reasons))
        elif not valid_book or spread > MAX_SPREAD or depth < MIN_DEPTH:
            status, why = "C_NO_TRADE", []
            if not valid_book: why.append("دفتر سفارش نامعتبر")
            if spread > MAX_SPREAD: why.append("اسپرد بیش از حد مجاز")
            if depth < MIN_DEPTH: why.append("عمق دفتر سفارش کمتر از حداقل است")
        elif not hard_ok:
            status, why = "C_NO_TRADE", ["سطح شکست معتبر یا نسبت ریسک به بازده مناسب وجود ندارد"]
        elif score < MIN_SCORE:
            status, why = "C_NO_TRADE", [f"امتیاز {score} کمتر از آستانه {MIN_SCORE} است"]
        else:
            status, why = "B_CONDITIONAL", []

        candidates.append({
            "side": side, "score": score, "status": status, "components": parts,
            "sampled_returns_pct": {"24h_from_open": r24},
            "conditional_plan": {
                "entry": entry, "entry_trigger": trigger, "stop_loss": sl,
                "take_profit_1": tp1, "take_profit_2": tp2, "rr_tp1": rr,
                "activation": "ورود فقط بعد از عبور قیمت از Entry Trigger و تثبیت؛ قبل از آن ورود ممنوع",
                "trigger_rule": "فقط پس از شکست و تثبیت بیرون سقف/کف ۲۴ساعته؛ سطح از market snapshot است نه کندل",
                "level_type": "lbank_24h_market_range_not_candle_structure"
            },
            "reasons": why
        })

    best = max(candidates, key=lambda x: (x["status"] == "B_CONDITIONAL", x["score"]))
    other = next(x for x in candidates if x["side"] != best["side"])
    return {
        "symbol": symbol, "base_asset": asset, "status": best["status"],
        "side": best["side"], "score": best["score"], "price": px,
        "snapshot_age_minutes": sa, "orderbook_age_minutes": ba,
        "market_context": {"returns_pct": {"24h": r24},
            "orderbook": {"best_bid": bid, "best_ask": ask, "spread_pct": spread,
                "bid_depth_quote": bd, "ask_depth_quote": ad, "total_depth_quote": depth,
                "depth_imbalance": imb}},
        "analysis": best, "alternate_side": other, "updated_at": t.isoformat(),
        "contract": {"priceTick": contract.get("priceTick"),
            "volumeMultiple": contract.get("volumeMultiple")}
    }

def main():
    t = now()
    prices, books = read_jsonl(f"{D}/prices.jsonl"), read_jsonl(f"{D}/orderbooks.jsonl")
    cp = read_json(f"{D}/contracts.json", {})
    contracts_raw = cp.get("contracts", []) if isinstance(cp, dict) else []
    snaps, bookmap = latest(prices), latest(books)
    contracts = {norm(c.get("symbol")): c for c in contracts_raw if isinstance(c, dict) and c.get("symbol")}
    results, missing = [], []
    for asset in WATCHLIST:
        matches = [(s,c) for s,c in contracts.items()
                   if base(c) == asset and str(c.get("clearCurrency") or QUOTE).upper() == QUOTE]
        if not matches:
            missing.append(asset)
            results.append({"symbol": asset+QUOTE, "base_asset": asset, "status": "INSUFFICIENT_DATA",
                "side": None, "score": 0, "price": None,
                "analysis": {"reasons": ["قرارداد USDT این دارایی در فهرست قراردادهای دریافتی نیست"], "conditional_plan": {}}})
            continue
        for sym, c in matches:
            snap = snaps.get(sym)
            results.append(analyze(str((snap or {}).get("symbol") or c.get("symbol") or sym),
                                   asset, snap, bookmap.get(sym), c, t))

    b = [x for x in results if x["status"] == "B_CONDITIONAL"]
    ins = [x for x in results if x["status"] == "INSUFFICIENT_DATA"]
    rej = [x for x in results if x["status"] == "C_NO_TRADE"]
    report = [
        "LBANK FUTURES SMART SCANNER - 20 ASSET WATCHLIST",
        f"Generated: {t.isoformat()}",
        "Data source: LBank Contract Futures public API only", "",
        f"Watchlist assets: {len(WATCHLIST)} ({', '.join(WATCHLIST)})",
        f"Markets analyzed: {len(results)}",
        "A - confirmed signals: 0 (DISABLED: no verified Futures candles)",
        f"B - conditional setups: {len(b)}",
        f"INSUFFICIENT_DATA: {len(ins)}",
        f"C - rejected: {len(rej)}", "",
        "IMPORTANT: These are conditional breakout plans, NOT market orders.",
        "No verified Futures OHLCV candles; EMA/RSI/MACD/structure indicators are unavailable.",
        "Entry is valid ONLY after the stated trigger is crossed and price holds beyond it.",
        "If price reaches SL before entry triggers, CANCEL the setup.",
        "Levels are derived from LBank 24h range snapshots, not candle support/resistance.",
        "Leverage and position sizing are disabled. Never use high leverage.", "",
        "CONDITIONAL TRADE PLANS (all prices are exact scanner references):"
    ]
    for x in sorted(b, key=lambda z:z["score"], reverse=True):
        p=x["analysis"]["conditional_plan"]
        report += [
            "",
            f"{x['symbol']} | {x['side']} | Score {x['score']}/100 | {x['status']}",
            f"Current price : {x['price']}",
            f"ENTRY         : {p.get('entry')}",
            f"TRIGGER       : {p.get('entry_trigger')}",
            f"STOP LOSS     : {p.get('stop_loss')}",
            f"TAKE PROFIT 1 : {p.get('take_profit_1')}",
            f"TAKE PROFIT 2 : {p.get('take_profit_2')}",
            f"Risk/Reward   : {p.get('rr_tp1')} (to TP1)",
            f"ACTIVATION    : {p.get('activation')}",
            "CANCEL RULE   : If SL is hit before entry activation, cancel; do not chase price."
        ]
    if not b:
        report.append("- No setup passed all current data and liquidity filters.")
    report += ["", "INSUFFICIENT DATA:"]
    for x in ins:
        report.append(f"- {x['symbol']}: {'; '.join(x.get('analysis',{}).get('reasons',[]))}")
    if not ins: report.append("- None")
    counts=Counter(r for x in rej for r in x.get("analysis",{}).get("reasons",[]))
    report += ["", "REJECTION REASONS:"]
    report += [f"- {k}: {v}" for k,v in counts.most_common(10)] or ["- None"]

    obj={"scanner":"LBank Futures Smart Scanner - 20 asset watchlist",
         "generated_at":t.isoformat(),"source":"LBank Contract Futures public REST API",
         "mode":"ANALYSIS_ONLY_NO_ORDERS","candles_available":False,
         "confirmed_signals_enabled":False,"leverage_enabled":False,
         "watchlist":WATCHLIST,"missing_watchlist_assets":missing,
         "summary":{"markets_analyzed":len(results),"A_confirmed":0,
             "B_conditional":len(b),"insufficient_data":len(ins),"C_no_trade":len(rej)},
         "results":results,
         "disclaimer":"Snapshot-only conditional research. Not candle-based support/resistance. No order execution."}
    with open("signals_futures_lbank.json","w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    with open("report.txt","w",encoding="utf-8") as f: f.write("\n".join(report)+"\n")
    plans=[{"symbol":x["symbol"],"status":x["status"],"side":x["side"],"score":x["score"],
            "plan":x.get("analysis",{}).get("conditional_plan",{})} for x in b]
    with open("plans_state.json","w",encoding="utf-8") as f: json.dump({"generated_at":t.isoformat(),"plans":plans},f,ensure_ascii=False,indent=2)
    fields=["timestamp","symbol","status","side","score","price","entry","entry_trigger","stop_loss","take_profit_1","take_profit_2","rr_tp1","reason"]
    exists=os.path.exists("signals_log.csv")
    with open("signals_log.csv","a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        if not exists: w.writeheader()
        for x in results:
            p=x.get("analysis",{}).get("conditional_plan",{})
            w.writerow({"timestamp":t.isoformat(),"symbol":x["symbol"],"status":x["status"],
                "side":x.get("side"),"score":x.get("score"),"price":x.get("price"),
                "entry":p.get("entry"),"entry_trigger":p.get("entry_trigger"),"stop_loss":p.get("stop_loss"),
                "take_profit_1":p.get("take_profit_1"),"take_profit_2":p.get("take_profit_2"),
                "rr_tp1":p.get("rr_tp1"),"reason":" | ".join(x.get("analysis",{}).get("reasons",[]))})
    print(json.dumps(obj["summary"],ensure_ascii=False))
    print("\n".join(report))

if __name__ == "__main__":
    main()
