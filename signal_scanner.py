#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LBank USDT-M Futures SMC/Liquidity/Fibonacci scanner.
Consumes ONLY verified LBank Futures OHLCV candles from data/candles.jsonl.
No Spot fallback, no synthetic candles, no order placement.

Accepted JSONL candle schema (one candle per line):
{
  "source":"lbank_futures_contract_api",
  "symbol":"BTCUSDT",
  "timeframe":"15m",
  "timestamp":"2026-11-25T09:15:00Z",  # candle OPEN time
  "open":59000, "high":59200, "low":58800, "close":59100, "volume":123.4,
  "closed":true
}
The collector must only write closed, genuine Futures candles. The scanner rejects
unknown sources, malformed candles, duplicates, stale series and incomplete history.
"""
import csv, json, math, os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

WATCHLIST = [x.strip().upper() for x in os.getenv(
    "LBANK_WATCHLIST",
    "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,AVAX,LINK,LTC,HYPE,SUI,AAVE,UNI,NEAR,ICP,RENDER,ONDO,PYTH,JUP"
).split(",") if x.strip()][:20]
QUOTE = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()
CANDLE_FILE = Path(os.getenv("LBANK_CANDLE_FILE", "data/candles.jsonl"))
CONTRACT_FILE = Path("data/contracts.json")
OUT = Path("report.txt")
MIN_CANDLES = {"15m": 120, "1h": 100, "4h": 80}
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
MAX_STALE_MIN = float(os.getenv("MAX_CANDLE_STALE_MIN", "35"))
RISK_REWARD_MIN = float(os.getenv("MIN_RR", "1.8"))
RISK_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.5"))

def num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def parse_dt(x):
    try:
        d=datetime.fromisoformat(str(x).replace("Z","+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except Exception:
        return None

def fmt(x, digits=8):
    if x is None: return "دریافت نشده"
    if isinstance(x, float): return f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return str(x)

def read_jsonl(path):
    rows=[]
    if not path.exists(): return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            x=json.loads(line)
            if isinstance(x,dict): rows.append(x)
        except Exception: pass
    return rows

def norm_symbol(s):
    return "".join(c for c in str(s or "").upper() if c.isalnum())

def load_candles():
    grouped=defaultdict(dict)
    rejected=defaultdict(int)
    for r in read_jsonl(CANDLE_FILE):
        sym=norm_symbol(r.get("symbol"))
        tf=str(r.get("timeframe") or r.get("interval") or "")
        src=str(r.get("source") or "").lower()
        ts=parse_dt(r.get("timestamp") or r.get("openTime"))
        vals=[num(r.get(k)) for k in ("open","high","low","close","volume")]
        if not sym or tf not in TF_SECONDS or src not in {
            "lbank_futures_contract_api","lbank_futures_trade_stream","lbank_futures_verified"
        } or ts is None or any(v is None for v in vals):
            rejected["invalid_or_unverified"]+=1; continue
        o,h,l,c,v=vals
        if min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h):
            rejected["invalid_ohlcv"]+=1; continue
        if r.get("closed") is not True:
            rejected["not_closed"]+=1; continue
        key=int(ts.timestamp())
        grouped[(sym,tf)][key]={"timestamp":ts,"open":o,"high":h,"low":l,"close":c,"volume":v}
    result={}
    for key, items in grouped.items():
        arr=[items[k] for k in sorted(items)]
        # Deduplicate by open timestamp and reject non-contiguous gaps from indicator windows.
        result[key]=arr
    return result,dict(rejected)

def ema(values, period):
    if len(values)<period: return None
    alpha=2/(period+1)
    v=sum(values[:period])/period
    for x in values[period:]: v=alpha*x+(1-alpha)*v
    return v

def rsi(values, period=14):
    if len(values)<=period: return None
    gains=[]; losses=[]
    for a,b in zip(values[-period-1:-1],values[-period:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def macd(values):
    if len(values)<35: return None,None,None
    # Build EMA series for stable MACD and signal values.
    def series(period):
        alpha=2/(period+1); out=[None]*(period-1)
        v=sum(values[:period])/period; out.append(v)
        for x in values[period:]:
            v=alpha*x+(1-alpha)*v; out.append(v)
        return out
    a,b=series(12),series(26)
    line=[x-y for x,y in zip(a,b) if x is not None and y is not None]
    sig=ema(line,9)
    return line[-1],sig,(line[-1]-sig if sig is not None else None)

def atr(candles, period=14):
    if len(candles)<period+1: return None
    trs=[]
    for p,c in zip(candles[-period-1:-1],candles[-period:]):
        trs.append(max(c["high"]-c["low"],abs(c["high"]-p["close"]),abs(c["low"]-p["close"])))
    return sum(trs)/len(trs)

def pivots(candles, left=3, right=3):
    highs=[]; lows=[]
    for i in range(left,len(candles)-right):
        c=candles[i]
        if c["high"]>=max(x["high"] for x in candles[i-left:i+right+1]):
            highs.append((i,c["high"]))
        if c["low"]<=min(x["low"] for x in candles[i-left:i+right+1]):
            lows.append((i,c["low"]))
    return highs,lows

def structure(candles):
    hs,ls=pivots(candles)
    if len(hs)<2 or len(ls)<2: return {"trend":"نامشخص","bos":"نامشخص","swing_highs":hs,"swing_lows":ls}
    hh=hs[-1][1]>hs[-2][1]; hl=ls[-1][1]>ls[-2][1]
    lh=hs[-1][1]<hs[-2][1]; ll=ls[-1][1]<ls[-2][1]
    trend="صعودی" if hh and hl else "نزولی" if lh and ll else "رنج/ترکیبی"
    last=candles[-1]["close"]
    bos="شکست صعودی" if last>hs[-1][1] else "شکست نزولی" if last<ls[-1][1] else "بدون شکست تأییدشده"
    prev_trend="صعودی" if hs[-2][1]>hs[-3][1] and ls[-2][1]>ls[-3][1] else "نزولی" if hs[-2][1]<hs[-3][1] and ls[-2][1]<ls[-3][1] else "رنج/ترکیبی"
    choch=("تغییر شخصیت صعودی" if prev_trend=="نزولی" and last>hs[-1][1] else
           "تغییر شخصیت نزولی" if prev_trend=="صعودی" and last<ls[-1][1] else "تغییر شخصیت تأییدنشده")
    return {"trend":trend,"bos":bos,"choch":choch,"swing_highs":hs,"swing_lows":ls}

def liquidity_sweep(candles):
    hs,ls=pivots(candles)
    if len(hs)<1 or len(ls)<1: return None
    last=candles[-1]; prev=candles[-2]
    sh=hs[-1][1]; sl=ls[-1][1]
    # Sweep requires wick through a confirmed swing and close back inside.
    if last["high"]>sh and last["close"]<sh and last["close"]<last["open"]:
        return {"side":"SHORT","level":sh,"kind":"جمع‌آوری نقدینگی بالای سقف و بازگشت زیر آن"}
    if last["low"]<sl and last["close"]>sl and last["close"]>last["open"]:
        return {"side":"LONG","level":sl,"kind":"جمع‌آوری نقدینگی زیر کف و بازگشت بالای آن"}
    # Last closed candle only; no retrospective signal.
    return None

def latest_fvg(c):
    found=[]
    for i in range(max(2,len(c)-40),len(c)):
        a,b=c[i-2],c[i]
        if b["low"]>a["high"]:
            found.append({"side":"LONG","low":a["high"],"high":b["low"],"index":i})
        if b["high"]<a["low"]:
            found.append({"side":"SHORT","low":b["high"],"high":a["low"],"index":i})
    return found[-1] if found else None

def fib_levels(c, side):
    hs,ls=pivots(c)
    if not hs or not ls: return None
    hi=hs[-1][1]; lo=ls[-1][1]
    if hi<=lo: return None
    span=hi-lo
    if side=="LONG":
        return {k:hi-span*k for k in (0.5,0.618,0.705,0.786)}
    return {k:lo+span*k for k in (0.5,0.618,0.705,0.786)}

def indicators(c):
    closes=[x["close"] for x in c]; vols=[x["volume"] for x in c]
    m,s,h=macd(closes)
    vmean=sum(vols[-20:])/20 if len(vols)>=20 else None
    return {"ema20":ema(closes,20),"ema50":ema(closes,50),"ema200":ema(closes,200),
            "rsi14":rsi(closes),"macd":m,"macd_signal":s,"macd_hist":h,
            "volume_ratio":(vols[-1]/vmean if vmean and vmean>0 else None),"atr14":atr(c)}

def equal_liquidity(c, tolerance=0.0015):
    hs,ls=pivots(c)
    eqh= eql=None
    if len(hs)>=2:
        a,b=hs[-2][1],hs[-1][1]
        if abs(a-b)/max(a,b)<=tolerance: eqh=(a+b)/2
    if len(ls)>=2:
        a,b=ls[-2][1],ls[-1][1]
        if abs(a-b)/max(a,b)<=tolerance: eql=(a+b)/2
    return {"equal_highs_liquidity":eqh,"equal_lows_liquidity":eql}

def order_block(c, side, lookback=30):
    # Last opposite candle before a displacement candle that closes beyond a recent swing.
    hs,ls=pivots(c)
    if len(c)<10: return None
    start=max(1,len(c)-lookback)
    for i in range(len(c)-2,start-1,-1):
        cur=c[i]
        prior_high=max(x["high"] for x in c[max(0,i-5):i])
        prior_low=min(x["low"] for x in c[max(0,i-5):i])
        displacement=(cur["close"]>prior_high and cur["close"]>cur["open"]) if side=="LONG" else (cur["close"]<prior_low and cur["close"]<cur["open"])
        if not displacement: continue
        for j in range(i-1,max(start-1,i-6),-1):
            x=c[j]
            opposite=(x["close"]<x["open"]) if side=="LONG" else (x["close"]>x["open"])
            if opposite:
                return {"low":x["low"],"high":x["high"],"index":j}
    return None

def analyze_tf(c):
    st=structure(c); ind=indicators(c); sweep=liquidity_sweep(c); fvg=latest_fvg(c)
    eq=equal_liquidity(c)
    return {"structure":st,"indicators":ind,"sweep":sweep,"fvg":fvg,
            "equal_liquidity":eq,"bullish_order_block":order_block(c,"LONG"),
            "bearish_order_block":order_block(c,"SHORT")}

def make_plan(side,c,analysis,htf):
    price=c[-1]["close"]; a=analysis["indicators"]; st=analysis["structure"]
    fib=fib_levels(c,side); sweep=analysis["sweep"]; fvg=analysis["fvg"]
    if not fib or not sweep or sweep["side"]!=side: return None
    # Require higher-timeframe context to agree or at least not strongly oppose.
    if side=="LONG" and htf["structure"]["trend"]=="نزولی": return None
    if side=="SHORT" and htf["structure"]["trend"]=="صعودی": return None
    # Require an actual 15m structure break in the trade direction.
    if side=="LONG" and st["bos"]!="شکست صعودی": return None
    if side=="SHORT" and st["bos"]!="شکست نزولی": return None
    # Entry zone must be near 0.618-0.705 retracement, optionally overlapping FVG.
    zlo,zhi=sorted((fib[0.618],fib[0.705]))
    entry=(zlo+zhi)/2
    if abs(price-entry)>max(a["atr14"]*1.5 if a["atr14"] else 0, price*0.012): return None
    if fvg and fvg["side"]==side:
        overlap=max(zlo,fvg["low"])<=min(zhi,fvg["high"])
    else: overlap=False
    # At least one momentum confirmation, not all indicators as hard veto.
    r=a["rsi14"]; mh=a["macd_hist"]; vr=a["volume_ratio"]
    momentum=(r is not None and ((side=="LONG" and 45<=r<=70) or (side=="SHORT" and 30<=r<=55)))
    macd_ok=mh is not None and ((side=="LONG" and mh>0) or (side=="SHORT" and mh<0))
    volume_ok=vr is not None and vr>=1.15
    if not (momentum or macd_ok or volume_ok or overlap): return None
    atrv=a["atr14"] or price*0.005
    stop=min(sweep["level"],price-0.8*atrv) if side=="LONG" else max(sweep["level"],price+0.8*atrv)
    risk=price-stop if side=="LONG" else stop-price
    if risk<=0: return None
    t1=price+1.8*risk if side=="LONG" else price-1.8*risk
    t2=price+2.5*risk if side=="LONG" else price-2.5*risk
    rr=abs(t1-price)/risk
    if rr<RISK_REWARD_MIN: return None
    return {"side":side,"entry_zone":[zlo,zhi],"entry_reference":entry,"stop_loss":stop,
            "take_profit_1":t1,"take_profit_2":t2,"rr_tp1":rr,
            "liquidity_sweep":sweep["kind"],"fib_618":fib[0.618],"fib_705":fib[0.705],
            "fvg_overlap":overlap,"volume_confirmed":volume_ok,
            "activation":"فقط پس از بازگشت به ناحیه ورود و تأیید کندل بسته‌شده؛ سفارش بازار بدون تأیید ممنوع"}

def analyze_asset(asset, candles):
    symbol=asset+QUOTE
    data={tf:candles.get((norm_symbol(symbol),tf),[]) for tf in ("15m","1h","4h")}
    issues=[]
    for tf,arr in data.items():
        if len(arr)<MIN_CANDLES[tf]: issues.append(f"تعداد کندل {tf} ناکافی است ({len(arr)}/{MIN_CANDLES[tf]})")
        if arr:
            age=(datetime.now(timezone.utc)-arr[-1]["timestamp"]).total_seconds()/60
            if age>MAX_STALE_MIN: issues.append(f"کندل {tf} قدیمی است ({age:.1f} دقیقه)")
            if len(arr)>2:
                gaps=sum(1 for a,b in zip(arr[-100:-1],arr[-99:]) if (b["timestamp"]-a["timestamp"]).total_seconds()>TF_SECONDS[tf]*1.5)
                if gaps: issues.append(f"در سری {tf} شکاف زمانی وجود دارد")
    if issues:
        return {"symbol":symbol,"status":"داده ناکافی","issues":issues}
    a15=analyze_tf(data["15m"]); a1=analyze_tf(data["1h"]); a4=analyze_tf(data["4h"])
    long=make_plan("LONG",data["15m"],a15,a4)
    short=make_plan("SHORT",data["15m"],a15,a4)
    if long and not short: status="سناریوی خرید مشروط"
    elif short and not long: status="سناریوی فروش مشروط"
    elif long and short: status="سیگنال متعارض؛ ورود نکن"
    else: status="ورود نکن؛ شروط نقدینگی/ساختار/فیبو کامل نیست"
    def tf_summary(x):
        i=x["indicators"]
        return {"روند":x["structure"]["trend"],"ساختار":x["structure"]["bos"],
                "CHoCH":x["structure"].get("choch","نامشخص"),
                "نقدینگی سقف/کف برابر":x["equal_liquidity"],
                "Order Block صعودی":x["bullish_order_block"],
                "Order Block نزولی":x["bearish_order_block"],
                "EMA20":i["ema20"],"EMA50":i["ema50"],"EMA200":i["ema200"],
                "RSI14":i["rsi14"],"MACD":i["macd"],"MACD_signal":i["macd_signal"],
                "MACD_hist":i["macd_hist"],"نسبت حجم به میانگین":i["volume_ratio"],
                "ATR14":i["atr14"],"شکار نقدینگی":x["sweep"],"آخرین FVG":x["fvg"]}
    return {"symbol":symbol,"status":status,"price":data["15m"][-1]["close"],
            "analysis":{"4h":tf_summary(a4),"1h":tf_summary(a1),"15m":tf_summary(a15)},
            "long_plan":long,"short_plan":short}

def main():
    candles,rejected=load_candles()
    contracts= json.loads(CONTRACT_FILE.read_text(encoding="utf-8")) if CONTRACT_FILE.exists() else {}
    contract_map={norm_symbol(x.get("symbol")):x for x in contracts.get("contracts",[]) if isinstance(x,dict)}
    results=[analyze_asset(a,candles) for a in WATCHLIST]
    valid=sum(1 for x in results if x["status"] not in ("داده ناکافی",))
    longs=sum(1 for x in results if x.get("long_plan"))
    shorts=sum(1 for x in results if x.get("short_plan"))
    lines=["اسکنر نقدینگی، اسمارت‌مانی و فیبوناچی — قراردادهای آتی LBank",
           f"زمان گزارش: {datetime.now(timezone.utc).isoformat()}",
           "منبع: فقط کندل‌های بسته‌شده و تأییدشده Futures؛ بدون Spot و بدون داده ساختگی",
           f"دارایی‌ها: {len(WATCHLIST)} | دارای داده کافی: {valid} | داده ناکافی: {len(WATCHLIST)-valid}",
           f"سناریوی خرید: {longs} | سناریوی فروش: {shorts}",
           f"ردیف‌های کندل نامعتبر/تأییدنشده: {sum(rejected.values())}",
           "روش: شکار نقدینگی ← تغییر ساختار ← FVG/Order Block و فیبوناچی ← تأیید اندیکاتورها",
           "توجه: سیگنال‌ها مشروط‌اند؛ هیچ سفارشی ثبت نمی‌شود. ریسک پیشنهادی هر معامله حداکثر ۰٫۵٪ سرمایه.",
           ""]
    for x in results:
        lines += ["",f"━━━━━━━━ {x['symbol']} ━━━━━━━━",f"وضعیت: {x['status']}"]
        if x["status"]=="داده ناکافی":
            lines += ["علت: "+"؛ ".join(x["issues"])]
            continue
        lines += [f"قیمت مرجع: {fmt(x['price'])}"]
        for tf,label in (("4h","۴ ساعته"),("1h","۱ ساعته"),("15m","۱۵ دقیقه")):
            z=x["analysis"][tf]
            lines += [f"روند {label}: {z['روند']} | ساختار: {z['ساختار']}",
                      f"EMA20/50/200: {fmt(z['EMA20'])} / {fmt(z['EMA50'])} / {fmt(z['EMA200'])}",
                      f"RSI: {fmt(z['RSI14'],2)} | MACD: {fmt(z['MACD'],6)} | هیستوگرام: {fmt(z['MACD_hist'],6)}",
                      f"نسبت حجم به میانگین ۲۰ کندل: {fmt(z['نسبت حجم به میانگین'],2)} | ATR: {fmt(z['ATR14'])}",
                      f"شکار نقدینگی: {z['شکار نقدینگی']['kind'] if z['شکار نقدینگی'] else 'مشاهده نشد'}",
                      f"FVG: {json.dumps(z['آخرین FVG'],ensure_ascii=False) if z['آخرین FVG'] else 'مشاهده نشد'}"]
        for key,label in (("long_plan","سناریوی خرید"),("short_plan","سناریوی فروش")):
            p=x.get(key)
            if p:
                lines += [f"{label}: فعال‌سازی مشروط",
                          f"ناحیه ورود: {fmt(p['entry_zone'][0])} تا {fmt(p['entry_zone'][1])}",
                          f"ورود مرجع: {fmt(p['entry_reference'])} | حد ضرر: {fmt(p['stop_loss'])}",
                          f"هدف اول: {fmt(p['take_profit_1'])} | هدف دوم: {fmt(p['take_profit_2'])} | RR هدف اول: {fmt(p['rr_tp1'],2)}",
                          f"دلیل: {p['liquidity_sweep']}؛ فیبوناچی ۰٫۶۱۸ تا ۰٫۷۰۵؛ هم‌پوشانی FVG: {'بله' if p['fvg_overlap'] else 'خیر'}",
                          f"شرط فعال‌سازی: {p['activation']}"]
        if not x.get("long_plan") and not x.get("short_plan"):
            lines.append("نتیجه: ورود نکن؛ شکار نقدینگی و تغییر ساختار هم‌جهت با تایم‌فریم بالاتر تأیید نشده است.")
    lines += ["","مدیریت ریسک: ریسک هر معامله حداکثر ۰٫۵٪ سرمایه؛ اهرم بالا استفاده نشود.",
              "محدودیت: این موتور فقط زمانی تحلیل می‌دهد که data/candles.jsonl شامل کندل واقعی بسته‌شده Futures باشد."]
    OUT.write_text("\n".join(lines)+"\n",encoding="utf-8")
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),"source":"verified LBank Futures OHLCV only",
             "mode":"analysis_only_no_orders","summary":{"assets":len(results),"data_sufficient":valid,
             "insufficient_data":len(WATCHLIST)-valid,"long_setups":longs,"short_setups":shorts},
             "rejected_candles":rejected,"results":results}
    Path("signals_futures_lbank.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n".join(lines))

if __name__=="__main__":
    main()
