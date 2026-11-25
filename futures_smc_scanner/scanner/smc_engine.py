"""Conservative multi-timeframe SMC analysis for USDT perpetual futures.

Analysis only. This module never submits orders and never calculates executable
position size. It expects closed candles supplied by the data client.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
import math

import pandas as pd

from .config import ScannerConfig
from .indicators import candles_to_df, compute_indicator_snapshot
from .models import Candle, CoinScanResult, DirectionSetup, MarketDerivativeContext


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str


@dataclass
class OrderBlock:
    direction: str
    low: float
    high: float
    index: int


@dataclass
class FairValueGap:
    direction: str
    low: float
    high: float
    index: int


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _snapshot_value(snapshot: dict, *names: str, default=None):
    """Accept the current indicator names and legacy aliases."""
    for name in names:
        if name in snapshot and snapshot[name] is not None:
            return snapshot[name]
    return default


def detect_swings(
    df: pd.DataFrame, left: int = 3, right: int = 3
) -> List[SwingPoint]:
    """Pivot is returned only after its right-hand confirmation bars exist."""
    result: List[SwingPoint] = []
    if df is None or len(df) < left + right + 1:
        return result

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    for i in range(left, len(df) - right):
        high_window = highs[i-left:i+right+1]
        low_window = lows[i-left:i+right+1]
        if highs[i] == high_window.max() and (high_window == highs[i]).sum() == 1:
            result.append(SwingPoint(i, float(highs[i]), "HIGH"))
        if lows[i] == low_window.min() and (low_window == lows[i]).sum() == 1:
            result.append(SwingPoint(i, float(lows[i]), "LOW"))

    return sorted(result, key=lambda x: x.index)


def detect_structure_bos_choch(
    df: pd.DataFrame, swings: List[SwingPoint]
) -> Tuple[str, List[str]]:
    """Determine swing structure and detect a close crossing the latest level."""
    if df is None or len(df) < 3:
        return "RANGE", ["Insufficient candles"]

    last_i = len(df) - 1
    # A swing needs three right-side bars to be confirmed with default settings.
    confirmed = [s for s in swings if s.index + 3 <= last_i]
    highs = [s for s in confirmed if s.kind == "HIGH"]
    lows = [s for s in confirmed if s.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return "RANGE", ["Insufficient confirmed swing points"]

    prev_close = float(df["close"].iloc[-2])
    close = float(df["close"].iloc[-1])
    ph, lh = highs[-2].price, highs[-1].price
    pl, ll = lows[-2].price, lows[-1].price
    bull_structure = lh > ph and ll > pl
    bear_structure = lh < ph and ll < pl

    bull_break = prev_close <= lh < close
    bear_break = prev_close >= ll > close
    events: List[str] = []

    if bull_break:
        events.append("CHoCH Bullish" if bear_structure else "BOS Bullish")
    if bear_break:
        events.append("CHoCH Bearish" if bull_structure else "BOS Bearish")

    if bull_break and not bear_break:
        return "BULLISH", events
    if bear_break and not bull_break:
        return "BEARISH", events
    if bull_structure and not bear_structure:
        return "BULLISH", ["Higher Highs and Higher Lows"]
    if bear_structure and not bull_structure:
        return "BEARISH", ["Lower Highs and Lower Lows"]
    return "RANGE", events or ["Mixed or ranging structure"]


def detect_liquidity_sweeps(
    df: pd.DataFrame,
    swings: List[SwingPoint],
    lookback_recent: int = 8,
    left: int = 3,
    right: int = 3,
) -> List[str]:
    """A sweep must pierce a previously confirmed pivot and close back inside."""
    sweeps: List[str] = []
    if df is None or len(df) < 3:
        return sweeps

    start = max(1, len(df) - lookback_recent)
    for i in range(start, len(df)):
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])
        close = float(df["close"].iloc[i])
        prior = [s for s in swings if s.index + right < i]

        for swing in [s for s in prior if s.kind == "HIGH"][-4:]:
            if high > swing.price and close < swing.price:
                sweeps.append(f"Buyside Sweep at {swing.price:.8g} bar {i}")
                break

        for swing in [s for s in prior if s.kind == "LOW"][-4:]:
            if low < swing.price and close > swing.price:
                sweeps.append(f"Sellside Sweep at {swing.price:.8g} bar {i}")
                break

    return sweeps


def detect_order_blocks_and_fvgs(
    df: pd.DataFrame,
    atr_value: Optional[float] = None,
) -> Tuple[List[OrderBlock], List[FairValueGap]]:
    """Detect simple displacement OBs and three-candle FVGs.

    These are candidate zones, not proof of institutional orders. Mitigation is
    checked against subsequent candles before a zone is returned.
    """
    obs: List[OrderBlock] = []
    fvgs: List[FairValueGap] = []
    if df is None or len(df) < 10:
        return obs, fvgs

    bodies = (df["close"] - df["open"]).abs()
    avg_body = float(bodies.iloc[-50:].mean())
    if not _finite(avg_body) or avg_body <= 0:
        return obs, fvgs
    min_gap = max(avg_body * 0.15, (float(atr_value) * 0.05 if _finite(atr_value) else 0.0))

    for i in range(1, len(df) - 1):
        prev_high = float(df["high"].iloc[i - 1])
        prev_low = float(df["low"].iloc[i - 1])
        next_high = float(df["high"].iloc[i + 1])
        next_low = float(df["low"].iloc[i + 1])

        if next_low - prev_high >= min_gap:
            low, high = prev_high, next_low
            # A bullish FVG is mitigated if a later low has fully traversed it.
            later_lows = df["low"].iloc[i + 2:]
            if len(later_lows) == 0 or float(later_lows.min()) > low:
                fvgs.append(FairValueGap("BULLISH", low, high, i))
        elif prev_low - next_high >= min_gap:
            low, high = next_high, prev_low
            later_highs = df["high"].iloc[i + 2:]
            if len(later_highs) == 0 or float(later_highs.max()) < high:
                fvgs.append(FairValueGap("BEARISH", low, high, i))

    displacement_threshold = avg_body * 1.5
    for i in range(1, len(df) - 1):
        o, c = float(df["open"].iloc[i]), float(df["close"].iloc[i])
        no, nc = float(df["open"].iloc[i + 1]), float(df["close"].iloc[i + 1])
        if abs(nc - no) < displacement_threshold:
            continue

        if c < o and nc > no:
            low, high, direction = float(df["low"].iloc[i]), float(df["high"].iloc[i]), "BULLISH"
            subsequent = df.iloc[i + 2:]
            mitigated = len(subsequent) > 0 and float(subsequent["low"].min()) <= low
        elif c > o and nc < no:
            low, high, direction = float(df["low"].iloc[i]), float(df["high"].iloc[i]), "BEARISH"
            subsequent = df.iloc[i + 2:]
            mitigated = len(subsequent) > 0 and float(subsequent["high"].max()) >= high
        else:
            continue

        if not mitigated:
            obs.append(OrderBlock(direction, low, high, i))

    return obs[-10:], fvgs[-10:]


def compute_premium_discount_and_fibs(
    df: pd.DataFrame,
    swings: List[SwingPoint],
    lookback: int = 80,
) -> Dict[str, Any]:
    if df is None or len(df) < 10:
        raise ValueError("Insufficient candles for dealing range")

    recent = df.iloc[-lookback:]
    range_high = float(recent["high"].max())
    range_low = float(recent["low"].min())
    if not _finite(range_high) or not _finite(range_low) or range_high <= range_low:
        raise ValueError("Invalid dealing range")

    equilibrium = (range_high + range_low) / 2.0
    price = float(df["close"].iloc[-1])
    zone = "DISCOUNT" if price < equilibrium else "PREMIUM" if price > equilibrium else "EQUILIBRIUM"

    confirmed = [s for s in swings if s.index + 3 < len(df)]
    highs = [s for s in confirmed if s.kind == "HIGH"]
    lows = [s for s in confirmed if s.kind == "LOW"]
    if not highs or not lows:
        raise ValueError("Insufficient confirmed Fibonacci swings")

    high_point, low_point = highs[-1], lows[-1]
    if low_point.index < high_point.index:
        leg_low, leg_high, leg_direction = low_point.price, high_point.price, "BULLISH"
    elif high_point.index < low_point.index:
        leg_low, leg_high, leg_direction = low_point.price, high_point.price, "BEARISH"
    else:
        raise ValueError("Invalid Fibonacci swing sequence")

    diff = leg_high - leg_low
    if diff <= 0:
        raise ValueError("Invalid Fibonacci range")

    result: Dict[str, Any] = {
        "range_high": range_high,
        "range_low": range_low,
        "eq_050": equilibrium,
        "zone": zone,
        "leg_high": leg_high,
        "leg_low": leg_low,
        "leg_direction": leg_direction,
    }
    for name, ratio in (("050", .5), ("0618", .618), ("0705", .705), ("0786", .786)):
        result[f"bull_fib_{name}"] = leg_high - ratio * diff
        result[f"bear_fib_{name}"] = leg_low + ratio * diff

    result["bull_ext_1272"] = leg_low + 1.272 * diff
    result["bull_ext_1618"] = leg_low + 1.618 * diff
    result["bear_ext_1272"] = leg_high - 1.272 * diff
    result["bear_ext_1618"] = leg_high - 1.618 * diff
    return result


def _empty_setup(
    direction: str,
    config: ScannerConfig,
    reasons: List[str],
    status: str = "INSUFFICIENT_DATA",
) -> DirectionSetup:
    risk_usdt = round(config.account_equity_usdt * config.risk_per_trade_pct / 100.0, 4)
    return DirectionSetup(
        direction=direction,
        status=status,
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
        no_trade_reasons=reasons,
    )


def _evaluate_direction(
    direction: str,
    ctx: Optional[MarketDerivativeContext],
    tf_snapshots: Dict[str, dict],
    tf_smc: Dict[str, dict],
    mtf_bias: Dict[str, str],
    market_tradeable: bool,
    market_block_reasons: List[str],
    config: ScannerConfig,
) -> DirectionSetup:
    required = ("4h", "1h", "15m")
    if ctx is None:
        return _empty_setup(direction, config, ["Perpetual market context unavailable"])
    if any(tf not in tf_snapshots or tf not in tf_smc for tf in required):
        return _empty_setup(direction, config, ["Required 4h, 1h and 15m analysis unavailable"])

    bullish = direction == "LONG"
    expected = "BULLISH" if bullish else "BEARISH"
    reasons = list(dict.fromkeys(market_block_reasons))
    rationale: List[str] = []
    score = 0.0

    if mtf_bias.get("4h") == expected and mtf_bias.get("1h") == expected:
        score += 20
        rationale.append("4h and 1h structure aligned")
    else:
        reasons.append("4h and 1h structure are not aligned with this direction")

    if mtf_bias.get("15m") == expected:
        score += 10
        rationale.append("15m structure aligned")
    else:
        reasons.append("15m structure does not confirm this direction")

    snap = tf_snapshots["15m"]
    smc = tf_smc["15m"]
    price = float(snap["close"])
    atr_value = float(_snapshot_value(snap, "atr14", "atr", default=0.0))
    rsi_value = float(_snapshot_value(snap, "rsi14", "rsi", default=50.0))
    ema20 = float(snap["ema20"])
    ema50 = float(snap["ema50"])
    ema200 = float(snap["ema200"])
    macd = float(snap["macd"])
    macd_signal = float(snap["macd_signal"])
    macd_hist = float(snap["macd_hist"])
    prev_hist = float(snap["prev_macd_hist"])
    vol_ratio = float(snap["vol_ratio"])

    if not all(_finite(x) for x in (price, atr_value, rsi_value, ema20, ema50, ema200, macd, macd_signal, macd_hist, prev_hist, vol_ratio)) or price <= 0 or atr_value <= 0:
        return _empty_setup(direction, config, ["Invalid or incomplete indicator values"])

    sweep_ok = any(("Sellside Sweep" in s) if bullish else ("Buyside Sweep" in s) for s in smc["sweeps"])
    if sweep_ok:
        score += 12
        rationale.append("Recent liquidity sweep detected")
    else:
        reasons.append("No directionally valid recent liquidity sweep")

    # Select the nearest zone below price for LONG or above price for SHORT.
    zones = []
    for ob in smc["obs"]:
        if ob.direction == ("BULLISH" if bullish else "BEARISH"):
            if bullish and ob.high < price and price - ob.high <= 2.5 * atr_value:
                zones.append((price - ob.high, ob.low, ob.high, "ORDER BLOCK"))
            elif not bullish and ob.low > price and ob.low - price <= 2.5 * atr_value:
                zones.append((ob.low - price, ob.low, ob.high, "ORDER BLOCK"))

    for fvg in smc["fvgs"]:
        if fvg.direction == ("BULLISH" if bullish else "BEARISH"):
            if bullish and fvg.high < price and price - fvg.high <= 2.5 * atr_value:
                zones.append((price - fvg.high, fvg.low, fvg.high, "FVG"))
            elif not bullish and fvg.low > price and fvg.low - price <= 2.5 * atr_value:
                zones.append((fvg.low - price, fvg.low, fvg.high, "FVG"))

    if not zones:
        return _empty_setup(direction, config, reasons + ["No nearby unmitigated directional OB/FVG"], "NO_TRADE")

    zones.sort(key=lambda x: x[0])
    _, entry_low, entry_high, zone_type = zones[0]
    if entry_low <= 0 or entry_high <= entry_low:
        return _empty_setup(direction, config, ["Invalid entry zone"], "NO_TRADE")
    score += 10
    rationale.append(f"Nearest candidate zone: {zone_type}")

    fibs = smc["fibs"]
    zone_ok = fibs["zone"] == ("DISCOUNT" if bullish else "PREMIUM")
    if zone_ok:
        score += 8
        rationale.append("Price in preferred Premium/Discount half")
    else:
        reasons.append("Price is not in preferred Premium/Discount zone")

    ema_ok = (
        ema20 > ema50 > ema200 and price > ema20
        if bullish else ema20 < ema50 < ema200 and price < ema20
    )
    if ema_ok:
        score += 10
        rationale.append("EMA20/50/200 alignment confirmed")
    else:
        reasons.append("EMA alignment not confirmed")

    rsi_ok = 45 <= rsi_value <= 70 if bullish else 30 <= rsi_value <= 55
    if rsi_ok:
        score += 7
        rationale.append(f"RSI momentum acceptable ({rsi_value:.1f})")
    else:
        reasons.append(f"RSI outside preferred range ({rsi_value:.1f})")

    macd_ok = (
        macd > macd_signal and macd_hist >= prev_hist
        if bullish else macd < macd_signal and macd_hist <= prev_hist
    )
    if macd_ok:
        score += 7
        rationale.append("MACD momentum confirms direction")
    else:
        reasons.append("MACD confirmation missing")

    if vol_ratio >= 1.0:
        score += 6
        rationale.append(f"Volume {vol_ratio:.2f}x 20-candle average")
    else:
        reasons.append(f"Volume below average ({vol_ratio:.2f}x)")

    entry_reference = (entry_low + entry_high) / 2.0

    if bullish:
        stop_loss = min(entry_low - .25 * atr_value, float(fibs["leg_low"]) - .2 * atr_value)
        if stop_loss <= 0 or stop_loss >= entry_reference:
            return _empty_setup(direction, config, ["Invalid LONG structural stop"], "NO_TRADE")
        risk_distance = entry_reference - stop_loss
        raw_targets = [float(fibs["bull_ext_1272"]), float(fibs["bull_ext_1618"])]
        raw_targets += [s.price for s in smc["swings"] if s.kind == "HIGH" and s.price > entry_reference]
        targets = sorted(set(t for t in raw_targets if _finite(t) and t > entry_reference))
    else:
        stop_loss = max(entry_high + .25 * atr_value, float(fibs["leg_high"]) + .2 * atr_value)
        if stop_loss <= entry_reference:
            return _empty_setup(direction, config, ["Invalid SHORT structural stop"], "NO_TRADE")
        risk_distance = stop_loss - entry_reference
        raw_targets = [float(fibs["bear_ext_1272"]), float(fibs["bear_ext_1618"])]
        raw_targets += [s.price for s in smc["swings"] if s.kind == "LOW" and 0 < s.price < entry_reference]
        targets = sorted(set(t for t in raw_targets if _finite(t) and 0 < t < entry_reference), reverse=True)

    if risk_distance <= 0:
        return _empty_setup(direction, config, ["Invalid risk distance"], "NO_TRADE")

    min_tp1 = config.min_rr_tp1 * risk_distance
    min_tp2 = config.min_rr_tp2 * risk_distance
    if bullish:
        targets = [t for t in targets if t - entry_reference >= min_tp1]
    else:
        targets = [t for t in targets if entry_reference - t >= min_tp1]

    if len(targets) < 2:
        return _empty_setup(direction, config, reasons + ["Two valid structural targets are unavailable"], "NO_TRADE")

    target_1, target_2 = targets[0], targets[1]
    rr1 = abs(target_1 - entry_reference) / risk_distance
    rr2 = abs(target_2 - entry_reference) / risk_distance
    if rr1 < config.min_rr_tp1 or rr2 < config.min_rr_tp2:
        reasons.append("Risk/reward below configured minimum")

    # A setup must still be waiting for its entry retracement.
    if bullish and price <= entry_high:
        reasons.append("Price is already at or inside the LONG entry zone; wait for a fresh setup")
    if not bullish and price >= entry_low:
        reasons.append("Price is already at or inside the SHORT entry zone; wait for a fresh setup")

    score = round(min(100.0, max(0.0, score)), 1)
    if score < config.min_confluence_score:
        reasons.append(f"Confluence {score} below threshold {config.min_confluence_score}")
    if not market_tradeable:
        reasons.extend(market_block_reasons)

    reasons = list(dict.fromkeys(reasons))
    status = "CONDITIONAL" if market_tradeable and not reasons else "NO_TRADE"

    if bullish:
        activation = [
            f"Price retraces into [{entry_low:.8g}, {entry_high:.8g}]",
            "A bullish 15m confirmation candle closes",
            "Revalidate spread, funding and market data before considering entry",
        ]
        invalidation = [
            f"Price reaches stop level {stop_loss:.8g} before entry",
            "A bearish 15m CHoCH invalidates the setup",
        ]
    else:
        activation = [
            f"Price retraces into [{entry_low:.8g}, {entry_high:.8g}]",
            "A bearish 15m confirmation candle closes",
            "Revalidate spread, funding and market data before considering entry",
        ]
        invalidation = [
            f"Price reaches stop level {stop_loss:.8g} before entry",
            "A bullish 15m CHoCH invalidates the setup",
        ]

    risk_usdt = round(config.account_equity_usdt * config.risk_per_trade_pct / 100.0, 4)
    return DirectionSetup(
        direction=direction,
        status=status,
        entry_low=round(entry_low, 10),
        entry_high=round(entry_high, 10),
        entry_reference=round(entry_reference, 10),
        stop_loss=round(stop_loss, 10),
        target_1=round(target_1, 10),
        target_2=round(target_2, 10),
        rr_tp1=round(rr1, 2),
        rr_tp2=round(rr2, 2),
        suggested_risk_pct=config.risk_per_trade_pct,
        suggested_risk_usdt=risk_usdt,
        suggested_position_size_base=None,
        confluence_score=score,
        rationale=rationale,
        activation_conditions=activation,
        invalidation_conditions=invalidation,
        no_trade_reasons=reasons,
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
    market_reasons = list(data_issues or [])
    required_tfs = ("15m", "1h", "4h")

    if ctx is None:
        market_reasons.append("Perpetual market context unavailable")
    elif not getattr(ctx, "is_real_perpetual", False):
        market_reasons.append("Instrument is not verified as a real perpetual contract")
    else:
        spread = getattr(ctx, "spread_pct", None)
        funding = getattr(ctx, "funding_rate", None)
        quote_volume = getattr(ctx, "quote_volume_24h", None)

        if spread is None or not _finite(spread):
            market_reasons.append("Order-book spread unavailable")
        elif float(spread) > config.max_spread_pct:
            market_reasons.append(f"Spread too high: {float(spread):.5f}%")

        if funding is not None and _finite(funding) and abs(float(funding)) > config.max_abs_funding_rate:
            market_reasons.append(f"Funding rate extreme: {float(funding):.6f}")

        if quote_volume is None or not _finite(quote_volume):
            market_reasons.append("24h quote volume unavailable")
        elif float(quote_volume) < config.min_24h_quote_volume_usdt:
            market_reasons.append("24h quote volume below configured minimum")

        # Reject stale context if timestamp fields are available.
        now_ms = __import__("time").time_ns() // 1_000_000
        price_ts = getattr(ctx, "price_fetched_at_ms", None) or getattr(ctx, "fetched_at_ms", None)
        book_ts = getattr(ctx, "orderbook_fetched_at_ms", None)
        if price_ts is None or now_ms - int(price_ts) > config.max_market_data_age_ms or now_ms < int(price_ts):
            market_reasons.append("Market price data missing, stale or future-dated")
        if book_ts is None or now_ms - int(book_ts) > config.max_orderbook_age_ms or now_ms < int(book_ts):
            market_reasons.append("Order-book data missing, stale or future-dated")

    for tf in required_tfs:
        candles = tf_candles.get(tf, [])
        minimum = max(int(config.min_candles_required), 220)
        if len(candles) < minimum:
            tf_bias[tf] = "UNAVAILABLE"
            continue

        try:
            df = candles_to_df(candles)
            if df is None or len(df) < 220:
                tf_bias[tf] = "UNAVAILABLE"
                continue

            snapshot = compute_indicator_snapshot(df)
            if snapshot is None:
                tf_bias[tf] = "UNAVAILABLE"
                continue

            # Normalize aliases from the current indicators implementation.
            if "atr14" not in snapshot and "atr" in snapshot:
                snapshot["atr14"] = snapshot["atr"]
            if "rsi14" not in snapshot and "rsi" in snapshot:
                snapshot["rsi14"] = snapshot["rsi"]

            swings = detect_swings(df)
            bias, events = detect_structure_bos_choch(df, swings)
            sweeps = detect_liquidity_sweeps(df, swings)
            atr_value = _snapshot_value(snapshot, "atr14", "atr", default=None)
            obs, fvgs = detect_order_blocks_and_fvgs(df, atr_value)

            fibs = compute_premium_discount_and_fibs(df, swings)

            tf_bias[tf] = bias
            tf_snapshots[tf] = snapshot
            tf_smc[tf] = {
                "swings": swings,
                "events": events,
                "sweeps": sweeps,
                "obs": obs,
                "fvgs": fvgs,
                "fibs": fibs,
            }
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
            tf_bias[tf] = "UNAVAILABLE"
            market_reasons.append(f"{tf} analysis failed: {type(exc).__name__}")

    missing = [tf for tf in required_tfs if tf not in tf_snapshots]
    if missing:
        market_reasons.append("Insufficient valid candle/indicator data: " + ", ".join(missing))

    if ctx is None or missing:
        market_tradeable = False
    else:
        market_tradeable = len(market_reasons) == 0

    if tf_bias.get("4h") == "BULLISH" and tf_bias.get("1h") == "BULLISH":
        mtf_alignment = "ALIGNED_BULLISH"
    elif tf_bias.get("4h") == "BEARISH" and tf_bias.get("1h") == "BEARISH":
        mtf_alignment = "ALIGNED_BEARISH"
    elif not any(v in ("BULLISH", "BEARISH") for v in tf_bias.values()):
        mtf_alignment = "UNAVAILABLE"
    else:
        mtf_alignment = "MIXED"

    long_setup = _evaluate_direction(
        "LONG", ctx, tf_snapshots, tf_smc, tf_bias,
        market_tradeable, market_reasons, config
    )
    short_setup = _evaluate_direction(
        "SHORT", ctx, tf_snapshots, tf_smc, tf_bias,
        market_tradeable, market_reasons, config
    )

    data_sufficient = ctx is not None and not missing
    data_valid = data_sufficient and not (data_issues or [])
    analysis_status = "AVAILABLE" if data_sufficient else "INSUFFICIENT_DATA"

    return CoinScanResult(
        symbol=symbol,
        exchange_source=ctx.exchange_source if ctx is not None else "none",
        market_tradeable=market_tradeable,
        data_valid=data_valid,
        timeframe_bias=tf_bias,
        mtf_alignment=mtf_alignment,
        funding_rate=getattr(ctx, "funding_rate", None) if ctx is not None else None,
        spread_pct=getattr(ctx, "spread_pct", None) if ctx is not None else None,
        long_setup=long_setup,
        short_setup=short_setup,
        data_issues=list(data_issues or []),
        data_sufficient=data_sufficient,
        analysis_status=analysis_status,
    )
