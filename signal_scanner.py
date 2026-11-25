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

    # Directional market bias from live LBank Futures snapshots and order book.
    # This is a market-context signal, not a candle-based technical signal.
    range_pos = ((px - lo) / (hi - lo)) if (px is not None and range_ok) else None
    funding = n(snap.get("prePositionFeeRate"))
    bias_score = 0
    bias_factors = {}

    def bias_add(name, value, points):
        nonlocal bias_score
        bias_factors[name] = {"value": value, "points": points}
        bias_score += points

    if r24 is not None and abs(r24) >= 0.15:
        bias_add("24h_momentum_pct", r24, 30 if r24 > 0 else -30)
    else:
        bias_add("24h_momentum_pct", r24, 0)

    if range_pos is not None:
        if range_pos >= 0.65:
            bias_add("24h_range_position", round(range_pos, 4), 20)
        elif range_pos <= 0.35:
            bias_add("24h_range_position", round(range_pos, 4), -20)
        else:
            bias_add("24h_range_position", round(range_pos, 4), 0)
    else:
        bias_add("24h_range_position", None, 0)

    if imb is not None and abs(imb) >= 0.08:
        bias_add("orderbook_imbalance", round(imb, 4), 25 if imb > 0 else -25)
    else:
        bias_add("orderbook_imbalance", imb, 0)

    # Funding is a small contrarian context factor, never a standalone signal.
    if funding is not None and abs(funding) >= 0.0001:
        bias_add("funding_rate", funding, -5 if funding > 0 else 5)
    else:
        bias_add("funding_rate", funding, 0)

    # A directional plan must never be produced from stale/invalid snapshot or book data.
    liquid_ok = bool(not reasons and valid_book and spread is not None
                     and spread <= MAX_SPREAD and depth >= MIN_DEPTH)
    bias_add("liquidity_filter", liquid_ok, 10 if liquid_ok else 0)

    bias_score = max(-100, min(100, bias_score))
    bias = "LONG_BIAS" if bias_score >= 25 else ("SHORT_BIAS" if bias_score <= -25 else "NEUTRAL")
    if any("snapshot قیمت نامعتبر یا قدیمی است" in r for r in reasons):
        bias, bias_score = "INSUFFICIENT_DATA", 0

    watch_plan = None
    watch_status = "NO_TRADE"
    if bias in ("LONG_BIAS", "SHORT_BIAS") and range_ok and px and liquid_ok:
        tk = n(contract.get("priceTick"))
        span = hi - lo
        if bias == "LONG_BIAS":
            e = tick_round(hi * 1.001, tk)
            s = tick_round(hi - 0.25 * span, tk)
            if px < e and px > s and e > s:
                risk = e - s
                watch_plan = {
                    "entry": e, "stop_loss": s,
                    "take_profit_1": tick_round(e + 1.5 * risk, tk),
                    "take_profit_2": tick_round(e + 2.0 * risk, tk),
                    "rr_tp1": 1.5, "rr_tp2": 2.0,
                    "basis": "24h range breakout reference; not candle structure"
                }
                watch_status = "WATCH_LONG"
            else:
                watch_status = "BREAKOUT_ALREADY_TRIGGERED_OR_INVALID"
        else:
            e = tick_round(lo * 0.999, tk)
            s = tick_round(lo + 0.25 * span, tk)
            if px > e and px < s and s > e:
                risk = s - e
                watch_plan = {
                    "entry": e, "stop_loss": s,
                    "take_profit_1": tick_round(e - 1.5 * risk, tk),
                    "take_profit_2": tick_round(e - 2.0 * risk, tk),
                    "rr_tp1": 1.5, "rr_tp2": 2.0,
                    "basis": "24h range breakout reference; not candle structure"
                }
                watch_status = "WATCH_SHORT"
            else:
                watch_status = "BREAKOUT_ALREADY_TRIGGERED_OR_INVALID"

    best = max(candidates, key=lambda x: (x["status"] == "B_CONDITIONAL", x["score"]))
    other = next(x for x in candidates if x["side"] != best["side"])
    return {
        "symbol": symbol, "base_asset": asset, "status": best["status"],
        "side": best["side"], "score": best["score"], "price": px,
        "snapshot_age_minutes": sa, "orderbook_age_minutes": ba,
        "market_context": {"returns_pct": {"24h": r24},
            "range_position_24h": range_pos, "funding_rate": funding,
            "orderbook": {"best_bid": bid, "best_ask": ask, "spread_pct": spread,
                "bid_depth_quote": bd, "ask_depth_quote": ad, "total_depth_quote": depth,
                "depth_imbalance": imb}},
        "directional_analysis": {
            "bias": bias, "bias_score": bias_score, "factors": bias_factors,
            "watch_status": watch_status, "conditional_plan": watch_plan,
            "note": "Bias uses live 24h Futures snapshot, funding and order book only; no EMA/RSI/MACD or candle structure."
        },
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
    watch = [x for x in results
             if x.get("directional_analysis", {}).get("conditional_plan")]
    long_bias = sum(1 for x in results if x.get("directional_analysis", {}).get("bias") == "LONG_BIAS")
    short_bias = sum(1 for x in results if x.get("directional_analysis", {}).get("bias") == "SHORT_BIAS")
    neutral_bias = sum(1 for x in results if x.get("directional_analysis", {}).get("bias") == "NEUTRAL")

    def fmt(v, digits=8):
        if v is None:
            return "دریافت نشده"
        if isinstance(v, bool):
            return "بله" if v else "خیر"
        if isinstance(v, (int, float)):
            return f"{v:.{digits}f}".rstrip("0").rstrip(".")
        return str(v)

    def bias_fa(value):
        return {
            "LONG_BIAS": "تمایل صعودی",
            "SHORT_BIAS": "تمایل نزولی",
            "NEUTRAL": "خنثی / بدون برتری مشخص",
        }.get(value, "داده ناکافی")

    def watch_fa(value):
        return {
            "WATCH_LONG": "سناریوی خرید مشروط",
            "WATCH_SHORT": "سناریوی فروش مشروط",
            "BREAKOUT_ALREADY_TRIGGERED_OR_INVALID": "سطح ورود رد شده یا نامعتبر است",
            "NO_TRADE": "فعلاً معامله نکن",
        }.get(value, "فعلاً معامله نکن")

    def explain_bias(x):
        d = x.get("directional_analysis", {})
        f = d.get("factors", {})
        bits = []
        mom = f.get("24h_momentum_pct", {}).get("value")
        pos = f.get("24h_range_position", {}).get("value")
        imb = f.get("orderbook_imbalance", {}).get("value")
        if mom is not None:
            bits.append(f"تغییر ۲۴ساعته {fmt(mom, 3)}٪")
        if pos is not None:
            bits.append(f"موقعیت در دامنه ۲۴ساعته {fmt(pos*100, 1)}٪")
        if imb is not None:
            bits.append(f"عدم‌تعادل دفتر سفارش {fmt(imb, 3)}")
        if not bits:
            return "داده کافی برای توضیح جهت بازار وجود ندارد."
        return "؛ ".join(bits) + "."

    report = [
        "اسکنر هوشمند قراردادهای آتی LBank - فهرست ۲۰ دارایی",
        f"زمان تولید گزارش: {t.isoformat()}",
        "منبع داده: فقط API عمومی قراردادهای آتی LBank",
        "",
        f"تعداد دارایی‌های فهرست: {len(WATCHLIST)} ({'، '.join(WATCHLIST)})",
        f"تعداد بازارهای بررسی‌شده: {len(results)}",
        "سیگنال تکنیکال قطعی: ۰ (تا زمان دسترسی به کندل معتبر Futures غیرفعال)",
        f"سناریوهای ورود مشروط قابل‌تحت‌نظر: {len(watch)}",
        f"داده ناکافی یا قدیمی: {len(ins)}",
        f"بازارهای بدون ستاپ معاملاتی: {len(rej)}",
        "",
        "خلاصه جهت بازار:",
        f"تمایل صعودی: {long_bias} | تمایل نزولی: {short_bias} | خنثی: {neutral_bias}",
        "",
        "محدودیت تحلیل:",
        "این نسخه از قیمت لحظه‌ای، تغییرات و دامنه ۲۴ساعته، Funding در صورت دریافت، و دفتر سفارش Futures استفاده می‌کند.",
        "به علت نبود کندل تأییدشده Futures، EMA، RSI، MACD، حجم کندلی و ساختار بازار محاسبه نمی‌شوند.",
        "امتیاز جهت بازار احتمال موفقیت معامله نیست و به‌تنهایی مجوز ورود محسوب نمی‌شود.",
        "قیمت‌های ورود و حدضرر این نسخه سناریوهای شکست دامنه ۲۴ساعته‌اند، نه حمایت و مقاومت کندلی.",
        "هیچ سفارش خودکاری ثبت نمی‌شود؛ اهرم و اندازه موقعیت باید جداگانه و با مدیریت ریسک تعیین شوند.",
        "",
        "سناریوهای ورود مشروط:"
    ]

    if not watch:
        report.append("در حال حاضر هیچ سناریوی ورود مشروطی از فیلترها عبور نکرده است.")
    for x in sorted(watch, key=lambda z: abs(z.get("directional_analysis", {}).get("bias_score", 0)), reverse=True):
        d = x.get("directional_analysis", {})
        p = d.get("conditional_plan") or {}
        side = "خرید (LONG)" if d.get("bias") == "LONG_BIAS" else "فروش (SHORT)"
        report += [
            "",
            f"{x['symbol']} | سناریوی {side}",
            f"قیمت فعلی: {fmt(x.get('price'))}",
            f"نقطه ورود مشروط: {fmt(p.get('entry'))}",
            f"حد ضرر: {fmt(p.get('stop_loss'))}",
            f"هدف اول: {fmt(p.get('take_profit_1'))}",
            f"هدف دوم: {fmt(p.get('take_profit_2'))}",
            f"نسبت سود به زیان تا هدف اول: {fmt(p.get('rr_tp1'), 2)}",
            "شرط فعال‌سازی: قیمت باید از سطح ورود عبور کند و بیرون دامنه ۲۴ساعته تثبیت شود؛ صرف لمس قیمت کافی نیست.",
            "ابطال سناریو: اگر پیش از فعال‌شدن ورود، قیمت به حد ضرر برسد یا داده‌ها کهنه شوند، سناریو باطل است.",
            "تذکر: این سطوح از دامنه ۲۴ساعته استخراج شده‌اند و باید پیش از معامله با نمودار زنده بررسی شوند."
        ]

    report += [
        "",
        "تحلیل تک‌تک دارایی‌ها:"
    ]
    for x in results:
        d = x.get("directional_analysis", {})
        mc = x.get("market_context", {})
        ob = mc.get("orderbook", {})
        p = d.get("conditional_plan") or {}
        ret = mc.get("returns_pct", {}).get("24h")
        pos = mc.get("range_position_24h")
        hi = n((snaps.get(norm(x["symbol"])) or {}).get("highPrice24h"))
        lo = n((snaps.get(norm(x["symbol"])) or {}).get("lowPrice24h"))
        report += [
            "",
            f"━━ {x['symbol']} ━━",
            f"جهت احتمالی: {bias_fa(d.get('bias'))}",
            f"امتیاز جهت بازار: {fmt(d.get('bias_score', 0), 0)} (مقیاس ۱۰۰- تا ۱۰۰+؛ احتمال موفقیت نیست)",
            f"وضعیت معامله: {watch_fa(d.get('watch_status'))}",
            f"قیمت فعلی: {fmt(x.get('price'))}",
            f"تغییر ۲۴ساعته: {fmt(ret, 3)}٪",
            f"جایگاه قیمت در دامنه ۲۴ساعته: {fmt(pos*100 if pos is not None else None, 1)}٪",
            f"بیشترین قیمت ۲۴ساعته: {fmt(hi)} | کمترین قیمت ۲۴ساعته: {fmt(lo)}",
            f"نرخ Funding: {fmt(mc.get('funding_rate'), 8)}",
            f"عدم‌تعادل دفتر سفارش: {fmt(ob.get('depth_imbalance'), 4)}",
            f"فاصله خرید و فروش (Spread): {fmt(ob.get('spread_pct'), 5)}٪",
            f"عمق کل دفتر سفارش: {fmt(ob.get('total_depth_quote'), 2)} تتر",
            f"سن داده قیمت: {fmt(x.get('snapshot_age_minutes'), 1)} دقیقه | سن دفتر سفارش: {fmt(x.get('orderbook_age_minutes'), 1)} دقیقه",
            f"دلیل جهت‌گیری: {explain_bias(x)}",
        ]
        if p:
            report += [
                f"ورود مشروط: {fmt(p.get('entry'))}",
                f"حد ضرر: {fmt(p.get('stop_loss'))}",
                f"هدف اول: {fmt(p.get('take_profit_1'))} | هدف دوم: {fmt(p.get('take_profit_2'))}",
                "اقدام: فقط پس از شکست و تثبیت؛ در غیر این صورت ورود نکن."
            ]
        else:
            why = x.get("analysis", {}).get("reasons", [])
            if why:
                report.append("علت عدم ارائه ورود: " + "؛ ".join(why))
            elif d.get("watch_status") == "BREAKOUT_ALREADY_TRIGGERED_OR_INVALID":
                report.append("علت عدم ارائه ورود: قیمت در موقعیت مناسبی نسبت به ورود یا حد ضرر نیست؛ تعقیب قیمت ممنوع.")
            else:
                report.append("اقدام: ورود نکن؛ فعلاً ستاپ معتبر با قیمت ورود و حد ضرر قابل دفاع وجود ندارد.")

    report += [
        "",
        "دارایی‌های دارای داده ناکافی:"
    ]
    for x in ins:
        why = x.get("analysis", {}).get("reasons", [])
        report.append(f"- {x['symbol']}: {'؛ '.join(why) if why else 'داده کافی نیست'}")
    if not ins:
        report.append("- هیچ‌کدام")

    counts = Counter(r for x in rej for r in x.get("analysis", {}).get("reasons", []))
    report += ["", "دلایل رد ستاپ‌ها:"]
    report += [f"- {k}: {v} مورد" for k, v in counts.most_common(15)] or ["- موردی ثبت نشده"]

    report += [
        "",
        "یادآوری مدیریت ریسک:",
        "ریسک هر معامله را حداکثر ۰٫۵٪ تا ۱٪ سرمایه نگه دار؛ مجموع ریسک معاملات باز را محدود کن.",
        "اهرم بالا می‌تواند باعث لیکویید شدن سریع شود. بدون تعیین اندازه موقعیت بر اساس فاصله حد ضرر وارد نشو."
    ]

    obj={"scanner":"LBank Futures Smart Scanner - 20 asset watchlist",
         "generated_at":t.isoformat(),"source":"LBank Contract Futures public REST API",
         "mode":"ANALYSIS_ONLY_NO_ORDERS","candles_available":False,
         "confirmed_signals_enabled":False,"leverage_enabled":False,
         "watchlist":WATCHLIST,"missing_watchlist_assets":missing,
         "summary":{"markets_analyzed":len(results),"confirmed_technical_signals":0,
             "conditional_watch_setups":len(watch),"insufficient_data":len(ins),"no_trade":len(rej),
             "long_bias":long_bias,"short_bias":short_bias,"neutral_bias":neutral_bias},
         "results":results,
         "disclaimer":"تحلیل مشروط مبتنی بر داده Snapshot و دفتر سفارش Futures؛ نه تحلیل کندلی قطعی و نه توصیه تضمینی. هیچ سفارشی ثبت نمی‌شود."}
    with open("signals_futures_lbank.json","w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    with open("report.txt","w",encoding="utf-8") as f: f.write("\n".join(report)+"\n")
    plans=[{"symbol":x["symbol"],
            "bias":x.get("directional_analysis",{}).get("bias"),
            "bias_score":x.get("directional_analysis",{}).get("bias_score"),
            "plan":x.get("directional_analysis",{}).get("conditional_plan",{})}
           for x in watch]
    with open("plans_state.json","w",encoding="utf-8") as f: json.dump({"generated_at":t.isoformat(),"plans":plans},f,ensure_ascii=False,indent=2)
    fields=["timestamp","symbol","status","side","score","price","entry","entry_trigger","stop_loss","take_profit_1","take_profit_2","rr_tp1","reason"]
    exists=os.path.exists("signals_log.csv")
    with open("signals_log.csv","a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        if not exists: w.writeheader()
        for x in results:
            p=x.get("directional_analysis",{}).get("conditional_plan") or x.get("analysis",{}).get("conditional_plan",{})
            w.writerow({"timestamp":t.isoformat(),"symbol":x["symbol"],"status":x["status"],
                "side":x.get("directional_analysis",{}).get("bias") or x.get("side"),
                "score":x.get("directional_analysis",{}).get("bias_score",x.get("score")),"price":x.get("price"),
                "entry":p.get("entry"),"entry_trigger":p.get("entry_trigger"),"stop_loss":p.get("stop_loss"),
                "take_profit_1":p.get("take_profit_1"),"take_profit_2":p.get("take_profit_2"),
                "rr_tp1":p.get("rr_tp1"),"reason":" | ".join(x.get("analysis",{}).get("reasons",[]))})
    print("خلاصه اسکن:", json.dumps(obj["summary"],ensure_ascii=False))
    print("\n".join(report))

if __name__ == "__main__":
    main()
