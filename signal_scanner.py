#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Smart Market Analyst V3 - Safe Rebuild

Safety policy:
- Never use Spot candles for Futures signals.
- Never assume an undocumented Futures endpoint.
- Fail closed until Futures OHLCV is verified.
- Keep LONG and SHORT analysis independent.
- Validate trade direction and risk/reward.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

APP_NAME = "Smart Market Analyst V3"

DEFAULT_SYMBOLS = (
    "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT,"
    "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,"
    "DOT_USDT,LTC_USDT,ATOM_USDT,NEAR_USDT,BCH_USDT"
)

MIN_SCORE = float(os.getenv("MIN_SCORE", "65"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "2"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "3"))
MIN_RR = float(os.getenv("MIN_RR", "1.5"))

MIN_CANDLES = 100
ATR_PERIOD = 14

TIMEFRAMES = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

REPORT_FILE = os.getenv("REPORT_FILE", "report.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("signal_scanner")


# ---------------------------------------------------------
# EXCEPTIONS
# ---------------------------------------------------------

class ScanError(Exception):
    """Recoverable scanner error."""


class DataSourceError(ScanError):
    """Unverified or invalid market data source."""


# ---------------------------------------------------------
# SAFE NUMERIC HELPERS
# ---------------------------------------------------------

def to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)

        if math.isfinite(number):
            return number

    except (TypeError, ValueError, OverflowError):
        pass

    return None


def safe_div(
    numerator: float,
    denominator: float,
    default: float = 0.0
) -> float:

    if denominator == 0 or not math.isfinite(denominator):
        return default

    result = numerator / denominator

    return result if math.isfinite(result) else default


def clamp(
    value: float,
    low: float,
    high: float
) -> float:

    return max(low, min(high, value))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ---------------------------------------------------------
# CANDLE DATA MODEL
# ---------------------------------------------------------

@dataclass
class Candles:

    timestamp: List[int]
    open: List[float]
    high: List[float]
    low: List[float]
    close: List[float]
    volume: List[float]

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_rows(cls, rows: List[Any]) -> "Candles":

        parsed = {}

        for row in rows:

            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue

            try:
                ts = int(float(row[0]))

                if ts > 10**11:
                    ts //= 1000

                o, h, l, c, v = (
                    float(x) for x in row[1:6]
                )

            except (TypeError, ValueError, OverflowError):
                continue

            values = (o, h, l, c, v)

            if not all(math.isfinite(x) for x in values):
                continue

            if min(o, h, l, c) <= 0:
                continue

            if h < max(o, c, l):
                continue

            if l > min(o, c, h):
                continue

            if v < 0:
                continue

            parsed[ts] = (ts, o, h, l, c, v)

        clean = [
            parsed[k]
            for k in sorted(parsed)
        ]

        if len(clean) < MIN_CANDLES:
            raise ScanError(
                "Insufficient valid candles: %d; required: %d"
                % (len(clean), MIN_CANDLES)
            )

        return cls(
            timestamp=[x[0] for x in clean],
            open=[x[1] for x in clean],
            high=[x[2] for x in clean],
            low=[x[3] for x in clean],
            close=[x[4] for x in clean],
            volume=[x[5] for x in clean],
        )


# ---------------------------------------------------------
# DATA SOURCE SAFETY
# ---------------------------------------------------------

def futures_data_source_verified() -> bool:
    """
    Intentionally returns False.

    Do not change this to True until:
    1. An official Futures OHLCV endpoint is identified.
    2. Its symbol format is verified.
    3. All four timeframes return valid Futures candles.
    4. Returned prices are cross-checked against Futures market data.
    5. A repeatable automated test passes.
    """

    return False


def fetch_futures_klines(
    symbol: str,
    timeframe: str,
    limit: int = 300
) -> Candles:

    raise DataSourceError(
        "Futures OHLCV source is not verified. "
        "Spot API fallback is deliberately disabled."
    )


def fetch_futures_price(symbol: str) -> float:

    raise DataSourceError(
        "Futures price source is not verified."
    )


def validate_market_data(
    candles: Candles,
    timeframe: str
) -> None:

    if timeframe not in TIMEFRAMES:
        raise ScanError("Unsupported timeframe: " + timeframe)

    if len(candles) < MIN_CANDLES:
        raise ScanError("Not enough candles")

    now = int(time.time())
    interval = TIMEFRAMES[timeframe]

    last_closed = candles.timestamp[-1]

    if last_closed + interval > now:
        raise ScanError("Latest candle is incomplete")

    age = now - (last_closed + interval)

    if age > interval * 3:
        raise ScanError(
            "Candle data is stale: %d seconds" % age
        )
# =========================================================
# PART 2 - INDICATORS AND MARKET STRUCTURE
# =========================================================

def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []

    alpha = 2.0 / (period + 1)
    result = [values[0]]

    for value in values[1:]:
        result.append(
            alpha * value + (1.0 - alpha) * result[-1]
        )

    return result


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return [50.0] * len(values)

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [50.0] * period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result.append(calculate(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        result.append(calculate(avg_gain, avg_loss))

    return result


def macd(
    values: List[float]
) -> Tuple[List[float], List[float], List[float]]:

    fast = ema(values, 12)
    slow = ema(values, 26)

    line = [
        fast[i] - slow[i]
        for i in range(len(values))
    ]

    signal = ema(line, 9)

    histogram = [
        line[i] - signal[i]
        for i in range(len(line))
    ]

    return line, signal, histogram


def atr(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 14
) -> List[float]:

    if not close:
        return []

    tr = [high[0] - low[0]]

    for i in range(1, len(close)):
        tr.append(max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1])
        ))

    result = []
    current = sum(tr[:period]) / min(period, len(tr))

    for i, value in enumerate(tr):
        if i < period:
            current = sum(tr[:i + 1]) / (i + 1)
        elif i == period:
            current = sum(tr[:period + 1]) / (period + 1)
        else:
            current = (
                current * (period - 1) + value
            ) / period

        result.append(current)

    return result


def volume_ratio(
    volume: List[float],
    recent_period: int = 3,
    baseline_period: int = 20
) -> float:

    required = recent_period + baseline_period

    if len(volume) < required:
        return 1.0

    recent = sum(volume[-recent_period:]) / recent_period

    baseline_values = volume[
        -(required):-recent_period
    ]

    baseline = sum(baseline_values) / len(baseline_values)

    if baseline <= 0:
        return 1.0

    return recent / baseline


def find_swings(
    high: List[float],
    low: List[float],
    strength: int = 3
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:

    swing_highs = []
    swing_lows = []

    for i in range(strength, len(high) - strength):

        if all(
            high[i] > high[i - k]
            for k in range(1, strength + 1)
        ) and all(
            high[i] >= high[i + k]
            for k in range(1, strength + 1)
        ):
            swing_highs.append((i, high[i]))

        if all(
            low[i] < low[i - k]
            for k in range(1, strength + 1)
        ) and all(
            low[i] <= low[i + k]
            for k in range(1, strength + 1)
        ):
            swing_lows.append((i, low[i]))

    return swing_highs, swing_lows


def structure_direction(
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]]
) -> int:

    if len(highs) < 2 or len(lows) < 2:
        return 0

    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]

    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]

    if higher_high and higher_low:
        return 1

    if lower_high and lower_low:
        return -1

    return 0


def detect_trend(
    candles: Candles,
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]]
) -> str:

    if len(candles) < 50:
        return "RANGE"

    closes = candles.close
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]

    direction = structure_direction(highs, lows)
    price = closes[-1]

    if (
        direction == 1
        and price > e20
        and e20 > e50
    ):
        return "UP"

    if (
        direction == -1
        and price < e20
        and e20 < e50
    ):
        return "DOWN"

    return "RANGE"


def build_timeframe_analysis(
    candles: Candles,
    timeframe: str
) -> Dict[str, Any]:

    closes = candles.close

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    rsi_values = rsi(closes)
    macd_line, macd_signal, macd_hist = macd(closes)

    atr_values = atr(
        candles.high,
        candles.low,
        closes
    )

    swing_highs, swing_lows = find_swings(
        candles.high,
        candles.low,
        strength=2 if timeframe == "4h" else 3
    )

    trend = detect_trend(
        candles,
        swing_highs,
        swing_lows
    )

    return {
        "timeframe": timeframe,
        "candles": candles,
        "price": closes[-1],
        "trend": trend,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi_values[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "macd_hist": macd_hist[-1],
        "atr": atr_values[-1],
        "volume_ratio": volume_ratio(candles.volume),
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "structure": structure_direction(
            swing_highs,
            swing_lows
        )
    }


def liquidity_levels(
    analysis: Dict[str, Any],
    price: float
) -> Dict[str, Optional[float]]:

    highs = [
        value for _, value in analysis["swing_highs"]
        if value > price
    ]

    lows = [
        value for _, value in analysis["swing_lows"]
        if value < price
    ]

    return {
        "buy_side_liquidity": min(highs) if highs else None,
        "sell_side_liquidity": max(lows) if lows else None
    }


def detect_market_regime(
    analysis: Dict[str, Any]
) -> str:

    trends = [
        analysis[tf]["trend"]
        for tf in ("4h", "1h", "15m")
    ]

    if trends.count("UP") >= 2:
        return "UPTREND"

    if trends.count("DOWN") >= 2:
        return "DOWNTREND"

    return "RANGE_OR_TRANSITION"
    if stop_pct > max_stop_pct:
        errors.append("STOP_TOO_WIDE")

    if rr_1 < min_rr:
        errors.append("TARGET_1_RR_TOO_LOW")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "rr_1": round(rr_1, 3),
        "rr_2": round(rr_2, 3),
        "stop_percent": round(stop_pct, 3),
        "risk_distance": round(risk, 10),
    }


def calculate_position_size(
    account_balance: float,
    entry: float,
    stop_loss: float,
    risk_pct: float = RISK_PCT,
    max_leverage: float = MAX_LEVERAGE,
) -> dict:
    """
    محاسبه اندازه پوزیشن بر اساس درصد ریسک حساب.
    خروجی صرفاً محاسباتی است و سفارش ثبت نمی‌کند.
    """

    balance = to_float(account_balance)
    entry = to_float(entry)
    stop = to_float(stop_loss)
    risk_pct = to_float(risk_pct)
    max_leverage = to_float(max_leverage)

    if balance <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("Invalid balance or prices")

    if not 0 < risk_pct <= 2:
        raise ValueError("Risk percentage must be between 0 and 2")

    if not 0 < max_leverage <= 5:
        raise ValueError("Leverage must be between 0 and 5")

    distance = abs(entry - stop)

    if distance <= 0:
        raise ValueError("Entry and stop cannot be equal")

    risk_amount = balance * (risk_pct / 100)

    quantity = risk_amount / distance
    notional = quantity * entry

    max_notional = balance * max_leverage

    if notional > max_notional:
        quantity = max_notional / entry
        notional = max_notional

        actual_risk = quantity * distance
    else:
        actual_risk = risk_amount

    return {
        "quantity": quantity,
        "notional": notional,
        "risk_amount": actual_risk,
        "risk_percent": (actual_risk / balance) * 100,
        "max_leverage": max_leverage,
    }


def market_structure_summary(
    candles: Candles,
    analysis: dict,
) -> dict:
    """خلاصه ساختار قیمت برای گزارش و بررسی سناریوها."""

    if len(candles) < MIN_CANDLES:
        raise ValueError("Insufficient candles")

    price = to_float(candles.close[-1])
    trend = str(analysis.get("trend", "UNKNOWN")).upper()

    levels = support_resistance(candles)

    support = nearest_level(
        levels["support"],
        price,
    )

    resistance = nearest_level(
        levels["resistance"],
        price,
    )

    return {
        "price": price,
        "trend": trend,
        "support": support,
        "resistance": resistance,
        "all_supports": levels["support"],
        "all_resistances": levels["resistance"],
    }


# ============================================================
# END OF PART 3/5
# ============================================================
# ============================================================
# PART 4/5 — LONG/SHORT SCENARIO ENGINE & SCORING
# ============================================================

def calculate_rsi_value(values: list, period: int = 14) -> float:
    """محاسبه RSI بدون وابستگی به کتابخانه خارجی."""

    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_ema_value(values: list, period: int) -> float:
    """محاسبه مقدار آخر EMA."""

    if not values or period <= 0:
        return 0.0

    multiplier = 2.0 / (period + 1.0)
    result = float(values[0])

    for value in values[1:]:
        result = (
            float(value) * multiplier
            + result * (1.0 - multiplier)
        )

    return result


def calculate_macd_values(values: list) -> dict:
    """محاسبه MACD و خط سیگنال."""

    if len(values) < 35:
        return {
            "macd": 0.0,
            "signal": 0.0,
            "histogram": 0.0,
        }

    fast_values = []
    slow_values = []

    fast = float(values[0])
    slow = float(values[0])

    fast_multiplier = 2.0 / 13.0
    slow_multiplier = 2.0 / 27.0

    for value in values:
        value = float(value)

        fast = (
            value * fast_multiplier
            + fast * (1.0 - fast_multiplier)
        )

        slow = (
            value * slow_multiplier
            + slow * (1.0 - slow_multiplier)
        )

        fast_values.append(fast)
        slow_values.append(slow)

    macd_line = [
        f - s
        for f, s in zip(fast_values, slow_values)
    ]

    signal_line = calculate_ema_series(
        macd_line,
        9,
    )

    macd_now = macd_line[-1]
    signal_now = signal_line[-1]

    return {
        "macd": macd_now,
        "signal": signal_now,
        "histogram": macd_now - signal_now,
    }


def calculate_ema_series(values: list, period: int) -> list:
    """محاسبه سری کامل EMA."""

    if not values:
        return []

    multiplier = 2.0 / (period + 1.0)
    result = [float(values[0])]

    for value in values[1:]:
        result.append(
            float(value) * multiplier
            + result[-1] * (1.0 - multiplier)
        )

    return result


def candle_volume_confirmation(candles: Candles) -> dict:
    """بررسی حجم آخرین کندل نسبت به میانگین حجم."""

    if len(candles) < 21:
        return {
            "ratio": 0.0,
            "confirmed": False,
        }

    previous_volumes = candles.volume[-21:-1]

    average_volume = (
        sum(previous_volumes) / len(previous_volumes)
        if previous_volumes
        else 0.0
    )

    current_volume = to_float(candles.volume[-1])

    ratio = (
        current_volume / average_volume
        if average_volume > 0
        else 0.0
    )

    return {
        "ratio": ratio,
        "confirmed": ratio >= 1.2,
    }


def evaluate_scenario(
    side: str,
    candles: Candles,
    analysis: dict,
    higher_tf_analysis: dict = None,
) -> dict:
    """
    ارزیابی مستقل سناریوی LONG یا SHORT.
    امتیاز بالا به‌تنهایی مجوز ورود نیست.
    """

    side = str(side).upper()

    if side not in ("LONG", "SHORT"):
        raise ValueError("Side must be LONG or SHORT")

    if len(candles) < MIN_CANDLES:
        return {
            "side": side,
            "score": 0,
            "eligible": False,
            "reasons": ["INSUFFICIENT_CANDLES"],
        }

    closes = candles.close
    highs = candles.high
    lows = candles.low

    price = to_float(closes[-1])

    ema20 = calculate_ema_value(closes, 20)
    ema50 = calculate_ema_value(closes, 50)
    ema200 = calculate_ema_value(closes, 200)

    rsi_value = calculate_rsi_value(closes, 14)
    macd_data = calculate_macd_values(closes)
    volume_data = candle_volume_confirmation(candles)

    atr_value = to_float(analysis.get("atr"))

    if atr_value <= 0:
        return {
            "side": side,
            "score": 0,
            "eligible": False,
            "reasons": ["ATR_UNAVAILABLE"],
        }

    swings = find_swings(
        highs,
        lows,
        strength=3,
    )

    swing_highs = swings.get("highs", [])
    swing_lows = swings.get("lows", [])

    recent_highs = [
        highs[i]
        for i in swing_highs
        if i < len(highs) - 3
    ][-2:]

    recent_lows = [
        lows[i]
        for i in swing_lows
        if i < len(lows) - 3
    ][-2:]

    score = 0
    reasons = []
    blockers = []

    # --------------------------------------------------------
    # 1. EMA trend
    # --------------------------------------------------------

    if side == "LONG":
        if price > ema20 > ema50:
            score += 15
            reasons.append("PRICE_ABOVE_EMA20_EMA50")

        if ema50 > ema200:
            score += 10
            reasons.append("EMA50_ABOVE_EMA200")

        if price < ema20:
            blockers.append("PRICE_BELOW_EMA20")

    else:
        if price < ema20 < ema50:
            score += 15
            reasons.append("PRICE_BELOW_EMA20_EMA50")

        if ema50 < ema200:
            score += 10
            reasons.append("EMA50_BELOW_EMA200")

        if price > ema20:
            blockers.append("PRICE_ABOVE_EMA20")

    # --------------------------------------------------------
    # 2. RSI momentum
    # --------------------------------------------------------

    if side == "LONG":
        if 50 <= rsi_value <= 68:
            score += 10
            reasons.append("RSI_BULLISH_RANGE")
        elif rsi_value > 75:
            blockers.append("RSI_OVERBOUGHT")
        elif rsi_value < 40:
            blockers.append("RSI_WEAK_FOR_LONG")

    else:
        if 32 <= rsi_value <= 50:
            score += 10
            reasons.append("RSI_BEARISH_RANGE")
        elif rsi_value < 25:
            blockers.append("RSI_OVERSOLD")
        elif rsi_value > 60:
            blockers.append("RSI_STRONG_AGAINST_SHORT")

    # --------------------------------------------------------
    # 3. MACD momentum
    # --------------------------------------------------------

    histogram = macd_data["histogram"]

    if side == "LONG":
        if histogram > 0:
            score += 10
            reasons.append("MACD_BULLISH")
        else:
            blockers.append("MACD_NOT_BULLISH")

    else:
        if histogram < 0:
            score += 10
            reasons.append("MACD_BEARISH")
        else:
            blockers.append("MACD_NOT_BEARISH")

    # --------------------------------------------------------
    # 4. Market structure
    # --------------------------------------------------------

    structure_confirmed = False

    if side == "LONG" and len(recent_highs) >= 2 and len(recent_lows) >= 2:
        higher_high = recent_highs[-1] > recent_highs[-2]
        higher_low = recent_lows[-1] > recent_lows[-2]

        if higher_high and higher_low:
            score += 15
            structure_confirmed = True
            reasons.append("HIGHER_HIGH_HIGHER_LOW")

    elif side == "SHORT" and len(recent_highs) >= 2 and len(recent_lows) >= 2:
        lower_high = recent_highs[-1] < recent_highs[-2]
        lower_low = recent_lows[-1] < recent_lows[-2]

        if lower_high and lower_low:
            score += 15
            structure_confirmed = True
            reasons.append("LOWER_HIGH_LOWER_LOW")

    if not structure_confirmed:
        blockers.append("MARKET_STRUCTURE_NOT_CONFIRMED")

    # --------------------------------------------------------
    # 5. Volume confirmation
    # --------------------------------------------------------

    if volume_data["confirmed"]:
        score += 10
        reasons.append("VOLUME_CONFIRMED")
    else:
        blockers.append("VOLUME_NOT_CONFIRMED")

    # --------------------------------------------------------
    # 6. Higher timeframe alignment
    # --------------------------------------------------------

    higher_trend = None

    if higher_tf_analysis:
        higher_trend = str(
            higher_tf_analysis.get("trend", "UNKNOWN")
        ).upper()

        if side == "LONG":
            if higher_trend in ("UP", "BULLISH", "UPTREND"):
                score += 15
                reasons.append("HIGHER_TF_BULLISH")
            else:
                blockers.append("HIGHER_TF_NOT_BULLISH")

        else:
            if higher_trend in ("DOWN", "BEARISH", "DOWNTREND"):
                score += 15
                reasons.append("HIGHER_TF_BEARISH")
            else:
                blockers.append("HIGHER_TF_NOT_BEARISH")

    else:
        blockers.append("HIGHER_TF_DATA_MISSING")

    # --------------------------------------------------------
    # 7. Price volatility sanity check
    # --------------------------------------------------------

    atr_percent = (atr_value / price) * 100 if price > 0 else 999

    if atr_percent <= 5:
        score += 5
        reasons.append("VOLATILITY_ACCEPTABLE")
    else:
        blockers.append("VOLATILITY_TOO_HIGH")

    score = min(score, 100)

    eligible = (
        score >= MIN_SCORE
        and len(blockers) == 0
    )

    return {
        "side": side,
        "score": score,
        "eligible": eligible,
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi_value,
        "macd": macd_data,
        "volume_ratio": volume_data["ratio"],
        "atr": atr_value,
        "atr_percent": atr_percent,
        "higher_timeframe_trend": higher_trend,
        "structure_confirmed": structure_confirmed,
        "reasons": reasons,
        "blockers": blockers,
    }


def evaluate_both_sides(
    candles: Candles,
    analysis: dict,
    higher_tf_analysis: dict = None,
) -> dict:
    """ارزیابی جداگانه LONG و SHORT."""

    long_result = evaluate_scenario(
        "LONG",
        candles,
        analysis,
        higher_tf_analysis,
    )

    short_result = evaluate_scenario(
        "SHORT",
        candles,
        analysis,
        higher_tf_analysis,
    )

    return {
        "LONG": long_result,
        "SHORT": short_result,
    }


# ============================================================
# END OF PART 4/5
# ============================================================

# ============================================================
# PART 5/5 — REPORTING, SCANNER & SAFE MAIN
# ============================================================

def format_price(value: float) -> str:
    """نمایش خوانای قیمت."""
    value = to_float(value)

    if value <= 0:
        return "N/A"

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:.4f}"

    return f"{value:.8f}"


def build_report(
    status: str,
    message: str,
    results: dict = None,
) -> str:
    """ساخت گزارش متنی ربات."""

    lines = [
        "=" * 60,
        APP_NAME,
        "=" * 60,
        f"Time: {utc_now()}",
        f"Status: {status}",
        f"Message: {message}",
        "",
    ]

    if results:
        for symbol, data in results.items():
            lines.extend([
                "-" * 50,
                f"Symbol: {symbol}",
            ])

            for side in ("LONG", "SHORT"):
                scenario = data.get(side)

                if not scenario:
                    continue

                lines.append(
                    f"{side}: Score={scenario.get('score', 0)}"
                )

                lines.append(
                    f"Eligible: {scenario.get('eligible', False)}"
                )

                lines.append(
                    f"Reasons: {', '.join(scenario.get('reasons', []))}"
                )

                lines.append(
                    f"Blockers: {', '.join(scenario.get('blockers', []))}"
                )

                lines.append("")

    lines.extend([
        "=" * 60,
        "Risk notice: High leverage can cause rapid liquidation.",
        "This program does not place orders.",
        "=" * 60,
    ])

    return "\n".join(lines)


def save_report(report: str) -> bool:
    """ذخیره گزارش در فایل."""

    try:
        with open(
            REPORT_FILE,
            "w",
            encoding="utf-8",
        ) as file:
            file.write(report)

        return True

    except OSError as exc:
        logging.error("Could not save report: %s", exc)
        return False


def print_report(report: str) -> None:
    """نمایش گزارش در ترمینال."""
    print(report)


def safe_shutdown(message: str, exit_code: int = 1) -> int:
    """
    توقف ایمن:
    در صورت نبود منبع تأییدشده Futures هیچ سیگنالی تولید نمی‌شود.
    """

    logging.warning(message)

    report = build_report(
        status="NO TRADE",
        message=message,
    )

    print_report(report)
    save_report(report)

    return exit_code


def scan_symbol(
    symbol: str,
    account_balance: float = 0.0,
) -> dict:
    """
    تحلیل یک نماد.
    این تابع فقط پس از تأیید منبع واقعی Futures قابل اجراست.
    """

    if not futures_data_source_verified():
        raise DataSourceError(
            "Futures OHLCV source is not verified"
        )

    # دریافت کندل‌ها باید از Adapter رسمی Futures انجام شود.
    candles_15m = fetch_futures_klines(
        symbol,
        "15m",
    )

    candles_1h = fetch_futures_klines(
        symbol,
        "1h",
    )

    validate_market_data(candles_15m, "15m")
    validate_market_data(candles_1h, "1h")

    analysis_15m = build_timeframe_analysis(
        candles_15m,
    )

    analysis_1h = build_timeframe_analysis(
        candles_1h,
    )

    scenarios = evaluate_both_sides(
        candles_15m,
        analysis_15m,
        analysis_1h,
    )

    output = {
        "LONG": scenarios["LONG"],
        "SHORT": scenarios["SHORT"],
    }

    for side in ("LONG", "SHORT"):
        scenario = output[side]

        if not scenario["eligible"]:
            continue

        plan = calculate_trade_levels(
            side,
            candles_15m.close[-1],
            candles_15m,
            analysis_15m,
        )

        validation = validate_trade_plan(plan)

        scenario["trade_plan"] = plan
        scenario["validation"] = validation

        if not validation["valid"]:
            scenario["eligible"] = False
            scenario["blockers"].extend(
                validation["errors"]
            )

        elif account_balance > 0:
            scenario["position"] = calculate_position_size(
                account_balance,
                plan["entry"],
                plan["stop_loss"],
            )

    return output


def run_scanner(symbols: list) -> dict:
    """اجرای اسکن نمادها."""

    if not futures_data_source_verified():
        raise DataSourceError(
            "NO TRADE — FUTURES DATA NOT VERIFIED"
        )

    results = {}

    for symbol in symbols:
        try:
            logging.info("Scanning %s", symbol)

            results[symbol] = scan_symbol(symbol)

        except Exception as exc:
            logging.exception(
                "Scan failed for %s",
                symbol,
            )

            results[symbol] = {
                "error": str(exc),
                "LONG": None,
                "SHORT": None,
            }

    return results


def main() -> int:
    """نقطه شروع برنامه."""

    parser = argparse.ArgumentParser(
        description=APP_NAME
    )

    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Comma-separated Futures symbols",
    )

    parser.add_argument(
        "--balance",
        type=float,
        default=0.0,
        help="Account balance for position sizing",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    symbols = [
        item.strip().upper()
        for item in args.symbols.split(",")
        if item.strip()
    ]

    if not symbols:
        return safe_shutdown(
            "No symbols configured",
            2,
        )

    if args.balance < 0:
        return safe_shutdown(
            "Invalid account balance",
            2,
        )

    # --------------------------------------------------------
    # SAFETY GATE
    # --------------------------------------------------------

    if not futures_data_source_verified():
        return safe_shutdown(
            "NO TRADE — FUTURES DATA NOT VERIFIED. "
            "Official Futures OHLCV endpoint and candle "
            "integrity have not been verified. "
            "Spot data fallback is disabled.",
            0,
        )

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    try:
        results = run_scanner(symbols)

        report = build_report(
            status="SCAN COMPLETED",
            message="Analysis completed. No orders were placed.",
            results=results,
        )

        print_report(report)
        save_report(report)

        return 0

    except DataSourceError as exc:
        return safe_shutdown(str(exc), 1)

    except Exception as exc:
        logging.exception("Fatal scanner error")

        return safe_shutdown(
            f"Scanner stopped due to error: {exc}",
            1,
        )


if __name__ == "__main__":
    sys.exit(main())


# ============================================================
# END OF PART 5/5
# ============================================================
