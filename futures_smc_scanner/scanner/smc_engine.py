from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from .config import ScannerConfig
from .indicators import candles_to_df, compute_indicator_snapshot
from .models import Candle, CoinScanResult, DirectionSetup, MarketDerivativeContext


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # "HIGH" or "LOW"


@dataclass
class OrderBlock:
    direction: str  # "BULLISH" or "BEARISH"
    low: float
    high: float
    index: int


@dataclass
class FairValueGap:
    direction: str  # "BULLISH" or "BEARISH"
    low: float
    high: float
    index: int


def detect_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> List[SwingPoint]:
    swings: List[SwingPoint] = []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    if n < left + right + 1:
        return swings

    for i in range(left, n - right):
        win_h = highs[i - left : i + right + 1]
        if highs[i] == np.max(win_h) and np.sum(win_h == highs[i]) == 1:
            swings.append(SwingPoint(index=i, price=float(highs[i]), kind="HIGH"))
        win_l = lows[i - left : i + right + 1]
        if lows[i] == np.min(win_l) and np.sum(win_l == lows[i]) == 1:
            swings.append(SwingPoint(index=i, price=float(lows[i]), kind="LOW"))

    swings.sort(key=lambda s: s.index)
    return swings


def detect_structure_bos_choch(df: pd.DataFrame, swings: List[SwingPoint]) -> Tuple[str, List[str]]:
    """Determines trend bias ('BULLISH', 'BEARISH', 'RANGE') and structural events (BOS / CHoCH)."""
    events: List[str] = []
    swing_highs = [s for s in swings if s.kind == "HIGH"]
    swing_lows = [s for s in swings if s.kind == "LOW"]

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "RANGE", ["Insufficient swing points for structural confirmation"]

    last_close = float(df["close"].iloc[-1])
    prev_sh = swing_highs[-2].price
    last_sh = swing_highs[-1].price
    prev_sl = swing_lows[-2].price
    last_sl = swing_lows[-1].price

    bull_structure = last_sh > prev_sh and last_sl > prev_sl
    bear_structure = last_sh < prev_sh and last_sl < prev_sl

    if last_close > last_sh:
        if bear_structure:
            events.append(f"CHoCH Bullish above {last_sh:.6g}")
        else:
            events.append(f"BOS Bullish above {last_sh:.6g}")
    if last_close < last_sl:
        if bull_structure:
            events.append(f"CHoCH Bearish below {last_sl:.6g}")
        else:
            events.append(f"BOS Bearish below {last_sl:.6g}")

    if bull_structure or (any("Bullish" in e for e in events) and not any("Bearish" in e for e in events)):
        return "BULLISH", events or ["Higher Highs & Higher Lows structure"]
    if bear_structure or (any("Bearish" in e for e in events) and not any("Bullish" in e for e in events)):
        return "BEARISH", events or ["Lower Highs & Lower Lows structure"]
    return "RANGE", events or ["Mixed swing highs/lows (consolidation)"]


def detect_liquidity_sweeps(df: pd.DataFrame, swings: List[SwingPoint], lookback_recent: int = 8) -> List[str]:
    """Detects wick sweeps beyond recent swing highs (sell-side liquidity above highs) or swing lows (buy-side below lows) that close back inside."""
    sweeps: List[str] = []
    n = len(df)
    if n < 3:
        return sweeps

    start_idx = max(1, n - lookback_recent)
    swing_highs = [s for s in swings if s.kind == "HIGH"]
    swing_lows = [s for s in swings if s.kind == "LOW"]

    for i in range(start_idx, n):
        h_i = float(df["high"].iloc[i])
        l_i = float(df["low"].iloc[i])
        c_i = float(df["close"].iloc[i])

        for sh in swing_highs[-4:]:
            if sh.index < i and h_i > sh.price and c_i < sh.price:
                sweeps.append(f"Buyside Liquidity Sweep above {sh.price:.6g} at bar {i}")
                break

        for sl in swing_lows[-4:]:
            if sl.index < i and l_i < sl.price and c_i > sl.price:
                sweeps.append(f"Sellside Liquidity Sweep below {sl.price:.6g} at bar {i}")
                break

    return sweeps


def detect_order_blocks_and_fvgs(df: pd.DataFrame) -> Tuple[List[OrderBlock], List[FairValueGap]]:
    obs: List[OrderBlock] = []
    fvgs: List[FairValueGap] = []
    n = len(df)
    if n < 5:
        return obs, fvgs

    # Fair Value Gaps (3-candle imbalance)
    for i in range(1, n - 1):
        h_prev = float(df["high"].iloc[i - 1])
        l_prev = float(df["low"].iloc[i - 1])
        h_next = float(df["high"].iloc[i + 1])
        l_next = float(df["low"].iloc[i + 1])

        if l_next > h_prev:
            fvgs.append(FairValueGap(direction="BULLISH", low=h_prev, high=l_next, index=i))
        elif h_next < l_prev:
            fvgs.append(FairValueGap(direction="BEARISH", low=h_next, high=l_prev, index=i))

    # Order Blocks (last opposite candle before impulsive displacement)
    body = (df["close"] - df["open"]).abs()
    avg_body = float(body.iloc[max(0, n - 50) : n].mean()) if n > 1 else 0.0
    impulse_thresh = max(avg_body * 1.6, 1e-12)

    for i in range(1, n - 1):
        o_i = float(df["open"].iloc[i])
        c_i = float(df["close"].iloc[i])
        l_i = float(df["low"].iloc[i])
        h_i = float(df["high"].iloc[i])

        c_next = float(df["close"].iloc[i + 1])
        o_next = float(df["open"].iloc[i + 1])
        imp_next = abs(c_next - o_next)

        if c_i < o_i and c_next > o_next and imp_next >= impulse_thresh:
            obs.append(OrderBlock(direction="BULLISH", low=l_i, high=h_i, index=i))
        elif c_i > o_i and c_next < o_next and imp_next >= impulse_thresh:
            obs.append(OrderBlock(direction="BEARISH", low=l_i, high=h_i, index=i))

    return obs[-6:], fvgs[-6:]


def compute_premium_discount_and_fibs(
    df: pd.DataFrame,
    swings: List[SwingPoint],
    lookback: int = 80,
) -> Dict[str, float]:
    recent = df.iloc[-lookback:] if len(df) >= lookback else df
    range_high = float(recent["high"].max())
    range_low = float(recent["low"].min())
    if range_high <= range_low:
        range_high = range_low * 1.001 + 1e-8

    eq_050 = range_low + 0.5 * (range_high - range_low)
    current_price = float(df["close"].iloc[-1])
    zone = "DISCOUNT" if current_price < eq_050 else ("PREMIUM" if current_price > eq_050 else "EQUILIBRIUM")

    # Identify swing leg for Fibonacci retracement (0.5, 0.618, 0.705, 0.786) and extensions (1.272, 1.618)
    swing_highs = [s for s in swings if s.kind == "HIGH"]
    swing_lows = [s for s in swings if s.kind == "LOW"]

    leg_high = swing_highs[-1].price if swing_highs else range_high
    leg_low = swing_lows[-1].price if swing_lows else range_low

    # Bullish leg: low -> high (retracements pull back down from leg_high toward leg_low)
    # Bearish leg: high -> low (retracements pull back up from leg_low toward leg_high)
    diff = max(abs(leg_high - leg_low), 1e-9)

    bull_fib_050 = leg_high - 0.50 * diff
    bull_fib_0618 = leg_high - 0.618 * diff
    bull_fib_0705 = leg_high - 0.705 * diff
    bull_fib_0786 = leg_high - 0.786 * diff
    bull_ext_1272 = leg_low + 1.272 * diff
    bull_ext_1618 = leg_low + 1.618 * diff

    bear_fib_050 = leg_low + 0.50 * diff
    bear_fib_0618 = leg_low + 0.618 * diff
    bear_fib_0705 = leg_low + 0.705 * diff
    bear_fib_0786 = leg_low + 0.786 * diff
    bear_ext_1272 = leg_high - 1.272 * diff
    bear_ext_1618 = leg_high - 1.618 * diff

    return {
        "range_high": range_high,
        "range_low": range_low,
        "eq_050": eq_050,
        "zone": zone,
        "leg_high": leg_high,
        "leg_low": leg_low,
        "bull_fib_050": bull_fib_050,
        "bull_fib_0618": bull_fib_0618,
        "bull_fib_0705": bull_fib_0705,
        "bull_fib_0786": bull_fib_0786,
        "bull_ext_1272": bull_ext_1272,
        "bull_ext_1618": bull_ext_1618,
        "bear_fib_050": bear_fib_050,
        "bear_fib_0618": bear_fib_0618,
        "bear_fib_0705": bear_fib_0705,
        "bear_fib_0786": bear_fib_0786,
        "bear_ext_1272": bear_ext_1272,
        "bear_ext_1618": bear_ext_1618,
    }


def _evaluate_direction(
    direction: str,
    ctx: MarketDerivativeContext,
    tf_snapshots: Dict[str, dict],
    tf_smc: Dict[str, dict],
    mtf_bias: Dict[str, str],
    market_tradeable: bool,
    market_block_reasons: List[str],
    config: ScannerConfig,
) -> DirectionSetup:
    risk_usdt = round(config.account_equity_usdt * (config.risk_per_trade_pct / 100.0), 4)
    no_trade_reasons: List[str] = list(market_block_reasons)
    rationale: List[str] = []

    exec_tf = "15m" if "15m" in tf_snapshots else list(tf_snapshots.keys())[0]
    snap = tf_snapshots.get(exec_tf)
    smc = tf_smc.get(exec_tf)

    if snap is None or smc is None:
        no_trade_reasons.append("Insufficient indicator or SMC data on execution timeframe")
        return DirectionSetup(
            direction=direction,
            status="NO TRADE",
            entry_low=None,
            entry_high=None,
            entry_reference=None,
            stop_loss=None,
            target_1=None,
            target_2=None,
            rr_tp1=None,
            rr_tp2=None,
            suggested_risk_pct=config.risk_per_trade_pct,
            suggested_risk_usdt=risk_usdt,
            suggested_position_size_base=None,
            confluence_score=0.0,
            rationale=[],
            activation_conditions=[],
            invalidation_conditions=[],
            no_trade_reasons=no_trade_reasons,
        )

    score = 0.0
    price = snap["close"]
    atr_val = max(snap["atr"], price * 1e-5)

    # 1. Multi-timeframe alignment & structure (max 30 pts)
    higher_tfs = [tf for tf in ["4h", "1h"] if tf in mtf_bias]
    aligned_htf = sum(1 for tf in higher_tfs if (tf_bias := mtf_bias[tf]) == ("BULLISH" if direction == "LONG" else "BEARISH"))
    exec_bias = mtf_bias.get(exec_tf, "RANGE")

    if aligned_htf == len(higher_tfs) and len(higher_tfs) > 0:
        score += 20.0
        rationale.append(f"هم‌جهتی کامل تایم‌فریم‌های بالاتر ({', '.join(higher_tfs)}) در جهت {direction}")
    elif aligned_htf >= 1:
        score += 10.0
        rationale.append(f"هم‌جهتی جزئی تایم‌فریم بالاتر ({aligned_htf}/{len(higher_tfs)}) در جهت {direction}")
    else:
        no_trade_reasons.append(f"عدم هم‌جهتی ساختار تایم‌فریم‌های بالاتر با {direction}")

    if exec_bias == ("BULLISH" if direction == "LONG" else "BEARISH"):
        score += 10.0
        rationale.append(f"ساختار {exec_tf} تاییدکننده {direction} ({', '.join(smc['events'])})")
    else:
        no_trade_reasons.append(f"ساختار {exec_tf} ({exec_bias}) با {direction} همسو نیست")

    # 2. Liquidity Sweep + OB/FVG + Premium/Discount + Fibs (max 35 pts)
    fibs = smc["fibs"]
    sweeps = smc["sweeps"]
    obs = smc["obs"]
    fvgs = smc["fvgs"]

    if direction == "LONG":
        has_sweep = any("Sellside" in s for s in sweeps)
        matching_obs = [o for o in obs if o.direction == "BULLISH" and o.high <= price * 1.03]
        matching_fvgs = [f for f in fvgs if f.direction == "BULLISH" and f.high <= price * 1.03]
        in_discount = fibs["zone"] in ("DISCOUNT", "EQUILIBRIUM")
        fib_levels = [fibs["bull_fib_050"], fibs["bull_fib_0618"], fibs["bull_fib_0705"], fibs["bull_fib_0786"]]
        fib_low = min(fib_levels)
        fib_high = max(fib_levels)
        tp1_raw = fibs["bull_ext_1272"]
        tp2_raw = fibs["bull_ext_1618"]
    else:
        has_sweep = any("Buyside" in s for s in sweeps)
        matching_obs = [o for o in obs if o.direction == "BEARISH" and o.low >= price * 0.97]
        matching_fvgs = [f for f in fvgs if f.direction == "BEARISH" and f.low >= price * 0.97]
        in_discount = fibs["zone"] in ("PREMIUM", "EQUILIBRIUM")
        fib_levels = [fibs["bear_fib_050"], fibs["bear_fib_0618"], fibs["bear_fib_0705"], fibs["bear_fib_0786"]]
        fib_low = min(fib_levels)
        fib_high = max(fib_levels)
        tp1_raw = fibs["bear_ext_1272"]
        tp2_raw = fibs["bear_ext_1618"]

    if has_sweep:
        score += 10.0
        rationale.append("ثبت Liquidity Sweep معتبر در جهت عکس حرکت اصلاحی")
    else:
        no_trade_reasons.append("Liquidity Sweep تاییدکننده در ساختار اخیر مشاهده نشد")

    if matching_obs or matching_fvgs:
        score += 10.0
        rationale.append(f"وجود زون معتبر SMC (OB: {len(matching_obs)}, FVG: {len(matching_fvgs)})")
    else:
        no_trade_reasons.append("عدم وجود Order Block یا Fair Value Gap هم‌جهت در محدوده منطقی")

    if in_discount:
        score += 8.0
        rationale.append(f"موقعیت قیمتی در محدوده {fibs['zone']} نسبت به تعادل ۰.۵")
    else:
        no_trade_reasons.append(f"قیمت در محدوده نامناسب ({fibs['zone']}) برای {direction} قرار دارد")

    # Check proximity to Fib retracement cluster (0.5 - 0.786)
    cluster_min = min(fib_low, price * (1 - 0.015))
    cluster_max = max(fib_high, price * (1 + 0.015))
    near_fib = (fib_low * 0.985 <= price <= fib_high * 1.015)
    if near_fib:
        score += 7.0
        rationale.append("قرارگیری در محدوده بازگشتی فیبوناچی (0.5 تا 0.786)")

    # 3. Indicators Confluence: EMA20/50/200, RSI, MACD, ATR, Volume (max 35 pts)
    ema_aligned = (
        (snap["ema20"] > snap["ema50"] > snap["ema200"] and price > snap["ema20"])
        if direction == "LONG"
        else (snap["ema20"] < snap["ema50"] < snap["ema200"] and price < snap["ema20"])
    )
    if ema_aligned:
        score += 12.0
        rationale.append("هم‌راستایی کامل میانگین‌های متحرک EMA20 > EMA50 > EMA200" if direction == "LONG" else "هم‌راستایی نزولی EMA20 < EMA50 < EMA200")
    else:
        no_trade_reasons.append("هم‌راستایی کامل EMA20/EMA50/EMA200 برقرار نیست")

    rsi_ok = (45.0 <= snap["rsi"] <= 72.0) if direction == "LONG" else (28.0 <= snap["rsi"] <= 55.0)
    if rsi_ok:
        score += 8.0
        rationale.append(f"وضعیت RSI ({snap['rsi']:.1f}) در محدوده مومنتوم سالم")
    else:
        no_trade_reasons.append(f"RSI ({snap['rsi']:.1f}) خارج از بازه بهینه ورود است")

    macd_ok = (snap["macd"] > snap["macd_signal"] and snap["macd_hist"] >= snap["prev_macd_hist"]) if direction == "LONG" else (
        snap["macd"] < snap["macd_signal"] and snap["macd_hist"] <= snap["prev_macd_hist"]
    )
    if macd_ok:
        score += 8.0
        rationale.append("مومنتوم MACD و هیستوگرام هم‌جهت با سناریو")
    else:
        no_trade_reasons.append("مومنتوم MACD هم‌جهت نیست")

    vol_ok = snap["vol_ratio"] >= 1.0
    if vol_ok:
        score += 7.0
        rationale.append(f"حجم معاملات ({snap['vol_ratio']:.2f}x میانگین ۲۰ کندل) تاییدکننده است")
    else:
        no_trade_reasons.append(f"حجم معاملات پایین‌تر از میانگین است ({snap['vol_ratio']:.2f}x)")

    confluence_score = round(min(100.0, max(0.0, score)), 1)

    # Construct concrete entry zone, SL, TP1 (1.272), TP2 (1.618) and R:R
    if direction == "LONG":
        zone_candidates = [price * 0.997, fib_low, fib_high]
        if matching_obs:
            zone_candidates.extend([matching_obs[-1].low, matching_obs[-1].high])
        entry_low = round(min(zone_candidates), 8)
        entry_high = round(max(min(zone_candidates) * 1.003, min(price, max(zone_candidates))), 8)
        if entry_high < entry_low:
            entry_low, entry_high = entry_high, entry_low
        entry_ref = round((entry_low + entry_high) / 2.0, 8)
        sl_candidate = min(entry_low - 1.2 * atr_val, fibs["leg_low"] - 0.3 * atr_val)
        stop_loss = round(max(sl_candidate, entry_ref * 0.85), 8)
        risk_dist = max(entry_ref - stop_loss, entry_ref * 0.002)
        target_1 = round(max(tp1_raw, entry_ref + config.min_rr_tp1 * risk_dist), 8)
        target_2 = round(max(tp2_raw, target_1 + 0.8 * risk_dist, entry_ref + config.min_rr_tp2 * risk_dist), 8)
    else:
        zone_candidates = [price * 1.003, fib_low, fib_high]
        if matching_obs:
            zone_candidates.extend([matching_obs[-1].low, matching_obs[-1].high])
        entry_low = round(min(zone_candidates), 8)
        entry_high = round(max(zone_candidates), 8)
        entry_ref = round((entry_low + entry_high) / 2.0, 8)
        sl_candidate = max(entry_high + 1.2 * atr_val, fibs["leg_high"] + 0.3 * atr_val)
        stop_loss = round(min(sl_candidate, entry_ref * 1.15), 8)
        risk_dist = max(stop_loss - entry_ref, entry_ref * 0.002)
        target_1 = round(min(tp1_raw, entry_ref - config.min_rr_tp1 * risk_dist), 8)
        target_2 = round(min(tp2_raw, target_1 - 0.8 * risk_dist, entry_ref - config.min_rr_tp2 * risk_dist), 8)

    rr_tp1 = round(abs(target_1 - entry_ref) / risk_dist, 2) if risk_dist > 0 else 0.0
    rr_tp2 = round(abs(target_2 - entry_ref) / risk_dist, 2) if risk_dist > 0 else 0.0
    pos_size_base = round(risk_usdt / risk_dist, 6) if risk_dist > 0 else None

    rr_ok = rr_tp1 >= config.min_rr_tp1 and rr_tp2 >= config.min_rr_tp2
    if not rr_ok:
        no_trade_reasons.append(f"نسبت ریسک به ریوارد (R:R TP1={rr_tp1}, TP2={rr_tp2}) کمتر از حداقل مجاز است")

    if confluence_score < config.min_confluence_score:
        no_trade_reasons.append(f"امتیاز همگرایی ({confluence_score}) کمتر از حد آستانه ({config.min_confluence_score}) است")

    status = "SIGNAL" if (market_tradeable and rr_ok and confluence_score >= config.min_confluence_score and not no_trade_reasons) else "NO TRADE"
    if status == "NO TRADE" and not no_trade_reasons:
        no_trade_reasons.append("شرایط ترکیبی معامله به حد نصاب نرسیده است")

    if direction == "LONG":
        activation_conds = [
            f"ورود قیمت به محدوده [{entry_low}, {entry_high}] و بسته شدن کندل تایید صعودی در {exec_tf}",
            "حفظ حمایت ساختاری و عدم شکست پرقدرت کف محدوده ورود",
        ]
        invalidation_conds = [
            f"بسته شدن کندل {exec_tf} زیر حد ضرر ({stop_loss}) یا شکست ساختار صعودی (CHoCH نزولی)",
            "افزایش غیرعادی Spread یا Funding Rate شدید منفی/مثبت در لحظه ورود",
        ]
    else:
        activation_conds = [
            f"ورود قیمت به محدوده [{entry_low}, {entry_high}] و بسته شدن کندل تایید نزولی در {exec_tf}",
            "حفظ مقاومت ساختاری و عدم عبور پایدار بالای سقف محدوده ورود",
        ]
        invalidation_conds = [
            f"بسته شدن کندل {exec_tf} بالای حد ضرر ({stop_loss}) یا شکست ساختار نزولی (CHoCH صعودی)",
            "افزایش غیرعادی Spread یا Funding Rate شدید در لحظه ورود",
        ]

    return DirectionSetup(
        direction=direction,
        status=status,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_reference=entry_ref,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        rr_tp1=rr_tp1,
        rr_tp2=rr_tp2,
        suggested_risk_pct=config.risk_per_trade_pct,
        suggested_risk_usdt=risk_usdt,
        suggested_position_size_base=pos_size_base,
        confluence_score=confluence_score,
        rationale=rationale,
        activation_conditions=activation_conds,
        invalidation_conditions=invalidation_conds,
        no_trade_reasons=no_trade_reasons if status == "NO TRADE" else [],
    )


def analyze_symbol(
    symbol: str,
    ctx: Optional[MarketDerivativeContext],
    tf_candles: Dict[str, List[Candle]],
    data_issues: List[str],
    config: ScannerConfig,
) -> CoinScanResult:
    tf_bias: Dict[str, str] = {}
    tf_snapshots: Dict[str, dict] = {}
    tf_smc: Dict[str, dict] = {}
    market_block_reasons: List[str] = []

    data_valid = ctx is not None and ctx.is_real_perpetual and len(data_issues) == 0

    for tf in config.timeframes:
        c_list = tf_candles.get(tf, [])
        if len(c_list) < config.min_candles_required:
            tf_bias[tf] = "UNAVAILABLE"
            continue
        df = candles_to_df(c_list)
        snap = compute_indicator_snapshot(c_list)
        swings = detect_swings(df, left=3, right=3)
        bias, events = detect_structure_bos_choch(df, swings)
        sweeps = detect_liquidity_sweeps(df, swings)
        obs, fvgs = detect_order_blocks_and_fvgs(df)
        fibs = compute_premium_discount_and_fibs(df, swings)

        tf_bias[tf] = bias
        if snap is not None:
            tf_snapshots[tf] = snap
        tf_smc[tf] = {
            "swings": swings,
            "events": events,
            "sweeps": sweeps,
            "obs": obs,
            "fvgs": fvgs,
            "fibs": fibs,
        }

    if not tf_snapshots:
        data_valid = False
        market_block_reasons.append("داده کندل کافی برای تحلیل در تایم‌فریم‌ها موجود نیست")

    funding_rate = ctx.funding_rate if ctx else None
    spread_pct = ctx.spread_pct if ctx else None

    if ctx is None:
        market_block_reasons.append("عدم دریافت اطلاعات قرارداد دائمی از صرافی")
    else:
        if spread_pct is not None and spread_pct > config.max_spread_pct:
            market_block_reasons.append(f"اسپرد سفارش‌نامه بالا ({spread_pct:.4f}% > {config.max_spread_pct}%) - بازار غیرقابل معامله")
        if funding_rate is not None and abs(funding_rate) > config.max_abs_funding_rate:
            market_block_reasons.append(f"نرخ فاندینگ افراطی ({funding_rate:.6f}) - ریسک لیکوییدیتی بالا")
        if ctx.quote_volume_24h is not None and ctx.quote_volume_24h < config.min_24h_quote_volume_usdt:
            market_block_reasons.append(f"حجم معاملات ۲۴ ساعته پایین ({ctx.quote_volume_24h:.0f} USDT)")
        if not data_valid:
            market_block_reasons.append("تشخیص داده ناقص یا عدم تایید کامل کندل‌ها")

    # MTF alignment summary
    biases_set = {v for v in tf_bias.values() if v in ("BULLISH", "BEARISH")}
    if tf_bias.get("4h") == "BULLISH" and tf_bias.get("1h") == "BULLISH":
        mtf_alignment = "ALIGNED_BULLISH"
    elif tf_bias.get("4h") == "BEARISH" and tf_bias.get("1h") == "BEARISH":
        mtf_alignment = "ALIGNED_BEARISH"
    elif not biases_set:
        mtf_alignment = "UNAVAILABLE"
    else:
        mtf_alignment = "MIXED"

    market_tradeable = data_valid and len(market_block_reasons) == 0

    long_setup = _evaluate_direction(
        direction="LONG",
        ctx=ctx if ctx else MarketDerivativeContext(
            symbol=symbol, exchange_source="none", mark_price=0.0, index_price=None,
            funding_rate=None, best_bid=None, best_ask=None, spread_pct=None,
            quote_volume_24h=None, fetched_at_ms=0, is_real_perpetual=False
        ),
        tf_snapshots=tf_snapshots,
        tf_smc=tf_smc,
        mtf_bias=tf_bias,
        market_tradeable=market_tradeable,
        market_block_reasons=market_block_reasons,
        config=config,
    )

    short_setup = _evaluate_direction(
        direction="SHORT",
        ctx=ctx if ctx else MarketDerivativeContext(
            symbol=symbol, exchange_source="none", mark_price=0.0, index_price=None,
            funding_rate=None, best_bid=None, best_ask=None, spread_pct=None,
            quote_volume_24h=None, fetched_at_ms=0, is_real_perpetual=False
        ),
        tf_snapshots=tf_snapshots,
        tf_smc=tf_smc,
        mtf_bias=tf_bias,
        market_tradeable=market_tradeable,
        market_block_reasons=market_block_reasons,
        config=config,
    )

    return CoinScanResult(
        symbol=symbol,
        exchange_source=ctx.exchange_source if ctx else "none",
        market_tradeable=market_tradeable,
        data_valid=data_valid,
        timeframe_bias=tf_bias,
        mtf_alignment=mtf_alignment,
        funding_rate=funding_rate,
        spread_pct=spread_pct,
        long_setup=long_setup,
        short_setup=short_setup,
        data_issues=data_issues,
    )
