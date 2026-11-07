#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LBANK AI FUTURES SCANNER
Hybrid ICT / SMC / Liquidity Model

Timeframes:
4H  : Higher timeframe bias
1H  : Market structure and liquidity pools
15M : Liquidity sweep and MSS confirmation
5M  : FVG / Order Block entry zone

Risk:
Maximum leverage: 2x
Margin mode: ISOLATED
Maximum planned account risk: 0.5%

IMPORTANT:
OHLCV-based liquidity is an estimate, not actual order-book liquidity.
This scanner does not execute trades.
"""

import os
import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OKX_URL = "https://www.okx.com/api/v5/market/candles"

CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT",
]

MAX_SNAPSHOT_AGE_MINUTES = 20
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75

MAX_RETRIES = 3
MAX_WORKERS = 5

MAX_LEVERAGE = 2
RISK_PER_TRADE_PERCENT = 0.5
MARGIN_MODE = "ISOLATED"

MIN_RR = 2.0
MIN_STOP_PERCENT = 0.15
MAX_STOP_PERCENT = 3.0

MIN_5M_VOLUME_RATIO = 1.0
MIN_15M_VOLUME_RATIO = 0.8

PIVOT_LEFT = 3
PIVOT_RIGHT = 3

EQUAL_LEVEL_ATR_TOLERANCE = 0.20
SWEEP_ATR_BUFFER = 0.05
STOP_ATR_BUFFER = 0.15

MAX_SWEEP_AGE_15M = 8
MAX_ENTRY_ZONE_AGE_5M = 20

MIN_SIGNAL_SCORE = 75
MIN_WATCH_SCORE = 45

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
}

BLOCKED_SYMBOLS = {
    "SOXSUSDT", "SOXLUSDT", "QQQUSDT",
    "TQQQUSDT", "SKHYNIXUSDT", "SKHYUSDT",
    "SNXXUSDT", "MUSTOCKUSDT",
}

HEADERS = {
    "User-Agent": "LBank-ICT-SMC-Liquidity-Scanner/13.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class SkipSymbol(Exception):
    pass


# ============================================================
# GENERAL HELPERS
# ============================================================

def number(value, default=None):

    try:
        if value is None:
            return default

        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def fmt(value):

    value = number(value)

    if value is None:
        return "N/A"

    return f"{value:,.8g}"


def safe_div(a, b):

    a = number(a)
    b = number(b)

    if a is None or b is None or b == 0:
        return None

    return a / b


def get_first(row, keys):

    if not isinstance(row, dict):
        return None

    for key in keys:
        if row.get(key) is not None:
            return row[key]

    return None


def parse_timestamp(value):

    if value is None:
        return None

    try:

        if isinstance(value, (int, float)):

            x = float(value)

            return x / 1000 if x > 1e12 else x

        text = str(value).strip()

        if text.isdigit():
            return parse_timestamp(int(text))

        dt = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()

    except Exception:
        return None


def candle_body(candle):

    return abs(candle[4] - candle[1])


def candle_range(candle):

    return candle[2] - candle[3]


def is_bullish_candle(candle):

    return candle[4] > candle[1]


def is_bearish_candle(candle):

    return candle[4] < candle[1]


# ============================================================
# INDICATORS
# ============================================================

def ema_series(values, period):

    out = [None] * len(values)

    if len(values) < period:
        return out

    current = sum(values[:period]) / period

    out[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):

        current = (
            values[i] * alpha
            + current * (1 - alpha)
        )

        out[i] = current

    return out


def rsi(values, period=14):

    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 50 if avg_gain == 0 else 100

    return 100 - 100 / (
        1 + avg_gain / avg_loss
    )


def macd(values):

    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    line = [
        f - s
        for f, s in zip(fast, slow)
        if f is not None and s is not None
    ]

    if len(line) < 9:
        return None, None, None

    signal_series = ema_series(line, 9)

    signal = signal_series[-1]

    if signal is None:
        return None, None, None

    return (
        line[-1],
        signal,
        line[-1] - signal
    )


def atr(candles, period=14):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        trs.append(max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        ))

    return sum(trs[-period:]) / period


# ============================================================
# LOAD LBANK SNAPSHOTS
# ============================================================

def load_latest_lbank():

    if not DATA_FILE.exists():

        raise RuntimeError(
            f"Data file not found: {DATA_FILE}"
        )

    latest = {}

    with DATA_FILE.open(
        "r",
        encoding="utf-8"
    ) as fh:

        for line_no, line in enumerate(fh, 1):

            if not line.strip():
                continue

            try:

                row = json.loads(line)

                symbol = str(
                    get_first(
                        row,
                        ["symbol", "instId", "instrument"]
                    ) or ""
                ).strip().upper()

                if symbol not in CORE_SYMBOLS:
                    continue

                price = number(get_first(
                    row,
                    [
                        "lastPrice",
                        "price",
                        "last",
                        "close",
                        "last_price"
                    ]
                ))

                turnover = number(get_first(
                    row,
                    [
                        "turnover24h",
                        "turnover",
                        "quoteVolume",
                        "quote_volume",
                        "volValue",
                        "amount",
                        "volume_usdt"
                    ]
                ))

                timestamp = parse_timestamp(get_first(
                    row,
                    [
                        "timestamp",
                        "ts",
                        "time",
                        "updated_at",
                        "updateTime"
                    ]
                ))

                if (
                    not symbol
                    or price is None
                    or price <= 0
                    or timestamp is None
                ):
                    continue

                item = {
                    "symbol": symbol,
                    "price": price,
                    "turnover": turnover or 0,
                    "timestamp": timestamp,
                }

                if (
                    symbol not in latest
                    or timestamp >= latest[symbol]["timestamp"]
                ):
                    latest[symbol] = item

            except Exception as exc:

                print(
                    f"Invalid JSONL line {line_no}: {exc}"
                )

    now = time.time()
    valid = {}

    for symbol, item in latest.items():

        age = (
            now - item["timestamp"]
        ) / 60

        if (
            -5 <= age <= MAX_SNAPSHOT_AGE_MINUTES
            and item["turnover"] >= MIN_TURNOVER
            and symbol not in BLOCKED_SYMBOLS
        ):
            valid[symbol] = item

    print(
        f"Fresh LBank snapshots: "
        f"{len(valid)}/{len(CORE_SYMBOLS)}"
    )

    return valid


# ============================================================
# OKX CANDLES
# ============================================================

def convert_symbol(symbol):

    if not symbol.endswith("USDT"):

        raise SkipSymbol(
            f"Unsupported symbol: {symbol}"
        )

    return f"{symbol[:-4]}-USDT-SWAP"


def get_candles(symbol, timeframe):

    params = {
        "instId": convert_symbol(symbol),
        "bar": INTERVALS[timeframe],
        "limit": "250",
    }

    last_error = None

    for attempt in range(MAX_RETRIES):

        try:

            response = SESSION.get(
                OKX_URL,
                params=params,
                timeout=15
            )

            response.raise_for_status()

            payload = response.json()

            if payload.get("code") != "0":

                raise SkipSymbol(
                    payload.get("msg", "OKX API error")
                )

            candles = []

            # OKX returns newest candles first.
            for row in reversed(payload.get("data", [])):

                if len(row) < 9:
                    continue

                # Only completed candles.
                if str(row[8]) != "1":
                    continue

                candle = [
                    number(row[0]),
                    number(row[1]),
                    number(row[2]),
                    number(row[3]),
                    number(row[4]),
                    number(row[5]),
                ]

                if all(
                    x is not None
                    for x in candle
                ):
                    candles.append(candle)

            if len(candles) < 205:

                raise SkipSymbol(
                    f"Insufficient completed {timeframe} candles: "
                    f"{len(candles)}"
                )

            return candles

        except SkipSymbol:
            raise

        except Exception as exc:

            last_error = exc

            time.sleep(1 + attempt)

    raise SkipSymbol(
        f"OKX data unavailable: {last_error}"
    )


# ============================================================
# CONFIRMED PIVOTS
# ============================================================

def find_pivots(
    candles,
    left=PIVOT_LEFT,
    right=PIVOT_RIGHT
):

    highs = []
    lows = []

    # A pivot is used only after its right-side candles
    # have closed. No future candle is used at decision time.

    for i in range(
        left,
        len(candles) - right
    ):

        high = candles[i][2]
        low = candles[i][3]

        left_highs = [
            candles[j][2]
            for j in range(i - left, i)
        ]

        right_highs = [
            candles[j][2]
            for j in range(i + 1, i + right + 1)
        ]

        left_lows = [
            candles[j][3]
            for j in range(i - left, i)
        ]

        right_lows = [
            candles[j][3]
            for j in range(i + 1, i + right + 1)
        ]

        if (
            high > max(left_highs)
            and high >= max(right_highs)
        ):

            highs.append({
                "index": i,
                "time": candles[i][0],
                "price": high,
            })

        if (
            low < min(left_lows)
            and low <= min(right_lows)
        ):

            lows.append({
                "index": i,
                "time": candles[i][0],
                "price": low,
            })

    return highs, lows


def find_equal_levels(
    pivots,
    tolerance
):

    levels = []

    for i in range(len(pivots)):

        first = pivots[i]

        for second in pivots[i + 1:]:

            if abs(
                first["price"] - second["price"]
            ) <= tolerance:

                level = (
                    first["price"] + second["price"]
                ) / 2

                levels.append({
                    "price": level,
                    "first_index": first["index"],
                    "second_index": second["index"],
                })

    # Remove approximate duplicates.
    unique = []

    for item in levels:

        if not any(
            abs(item["price"] - x["price"]) <= tolerance
            for x in unique
        ):

            unique.append(item)

    return unique


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def timeframe_analysis(candles):

    closes = [c[4] for c in candles]
    volumes = [c[5] for c in candles]

    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)

    current = closes[-1]

    rsi_value = rsi(closes)

    macd_value, macd_signal, macd_hist = macd(closes)

    avg_volume = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21
        else None
    )

    volume_ratio = (
        safe_div(volumes[-1], avg_volume)
        if avg_volume
        else None
    )

    current_atr = atr(candles)

    bullish = (
        ema20[-1] is not None
        and ema50[-1] is not None
        and ema200[-1] is not None
        and current > ema20[-1]
        and ema20[-1] > ema50[-1]
        and ema50[-1] > ema200[-1]
    )

    bearish = (
        ema20[-1] is not None
        and ema50[-1] is not None
        and ema200[-1] is not None
        and current < ema20[-1]
        and ema20[-1] < ema50[-1]
        and ema50[-1] < ema200[-1]
    )

    highs, lows = find_pivots(candles)

    recent_highs = [
        x["price"] for x in highs[-10:]
    ]

    recent_lows = [
        x["price"] for x in lows[-10:]
    ]

    support = (
        max(
            [x for x in recent_lows if x < current],
            default=min(c[3] for c in candles[-50:])
        )
    )

    resistance = (
        min(
            [x for x in recent_highs if x > current],
            default=max(c[2] for c in candles[-50:])
        )
    )

    atr_value = current_atr or 0

    equal_highs = find_equal_levels(
        highs[-15:],
        atr_value * EQUAL_LEVEL_ATR_TOLERANCE
    )

    equal_lows = find_equal_levels(
        lows[-15:],
        atr_value * EQUAL_LEVEL_ATR_TOLERANCE
    )

    return {
        "price": current,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "volume_ratio": volume_ratio,
        "atr": current_atr,
        "bullish": bullish,
        "bearish": bearish,
        "support": support,
        "resistance": resistance,
        "pivots_high": highs,
        "pivots_low": lows,
        "equal_highs": equal_highs,
        "equal_lows": equal_lows,
    }


# ============================================================
# HIGHER TIMEFRAME BIAS
# ============================================================

def get_market_bias(data):

    h4 = data["4h"]
    h1 = data["1h"]

    if h4["bullish"] and h1["bullish"]:
        return "BULLISH"

    if h4["bearish"] and h1["bearish"]:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(
    candles,
    timeframe_data,
    direction
):

    highs = timeframe_data["pivots_high"]
    lows = timeframe_data["pivots_low"]

    atr_value = timeframe_data["atr"] or 0

    if atr_value <= 0:
        return None

    start = max(
        0,
        len(candles) - MAX_SWEEP_AGE_15M - 1
    )

    for i in range(
        len(candles) - 1,
        start - 1,
        -1
    ):

        candle = candles[i]

        # Only pivots confirmed before the sweep candle.
        previous_highs = [
            x for x in highs
            if x["index"] < i
        ]

        previous_lows = [
            x for x in lows
            if x["index"] < i
        ]

        if direction == "SHORT":

            levels = [
                x["price"]
                for x in previous_highs[-12:]
            ]

            levels += [
                x["price"]
                for x in timeframe_data["equal_highs"]
                if x["second_index"] < i
            ]

            for level in sorted(
                set(levels),
                reverse=True
            ):

                swept = (
                    candle[2] > level + atr_value * SWEEP_ATR_BUFFER
                    and candle[4] < level
                )

                if swept:

                    return {
                        "direction": direction,
                        "index": i,
                        "time": candle[0],
                        "level": level,
                        "wick": candle[2],
                        "type": "BSL SWEEP",
                    }

        else:

            levels = [
                x["price"]
                for x in previous_lows[-12:]
            ]

            levels += [
                x["price"]
                for x in timeframe_data["equal_lows"]
                if x["second_index"] < i
            ]

            for level in sorted(levels):

                swept = (
                    candle[3] < level - atr_value * SWEEP_ATR_BUFFER
                    and candle[4] > level
                )

                if swept:

                    return {
                        "direction": direction,
                        "index": i,
                        "time": candle[0],
                        "level": level,
                        "wick": candle[3],
                        "type": "SSL SWEEP",
                    }

    return None


# ============================================================
# MSS / BOS / CHoCH
# ============================================================

def detect_structure_shift(
    candles,
    timeframe_data,
    sweep
):

    if not sweep:
        return None

    direction = sweep["direction"]
    sweep_index = sweep["index"]

    highs = timeframe_data["pivots_high"]
    lows = timeframe_data["pivots_low"]

    # Structure pivots must have been confirmed before sweep.
    prior_highs = [
        x for x in highs
        if x["index"] < sweep_index
    ]

    prior_lows = [
        x for x in lows
        if x["index"] < sweep_index
    ]

    if direction == "LONG":

        if not prior_highs:
            return None

        broken_level = prior_highs[-1]["price"]

        for i in range(
            sweep_index + 1,
            len(candles)
        ):

            candle = candles[i]

            if candle[4] > broken_level:

                return {
                    "direction": "LONG",
                    "index": i,
                    "time": candle[0],
                    "level": broken_level,
                    "type": "BULLISH MSS",
                }

    else:

        if not prior_lows:
            return None

        broken_level = prior_lows[-1]["price"]

        for i in range(
            sweep_index + 1,
            len(candles)
        ):

            candle = candles[i]

            if candle[4] < broken_level:

                return {
                    "direction": "SHORT",
                    "index": i,
                    "time": candle[0],
                    "level": broken_level,
                    "type": "BEARISH MSS",
                }

    return None


# ============================================================
# FVG
# ============================================================

def detect_fvgs(candles, direction, start_index=0):

    zones = []

    start = max(
        2,
        start_index
    )

    for i in range(
        start,
        len(candles)
    ):

        first = candles[i - 2]
        middle = candles[i - 1]
        third = candles[i]

        middle_range = candle_range(middle)

        if middle_range <= 0:
            continue

        if direction == "LONG":

            # Bullish three-candle imbalance.
            if first[2] < third[3]:

                zones.append({
                    "low": first[2],
                    "high": third[3],
                    "index": i,
                    "time": third[0],
                    "type": "BULLISH FVG",
                })

        else:

            # Bearish three-candle imbalance.
            if first[3] > third[2]:

                zones.append({
                    "low": third[2],
                    "high": first[3],
                    "index": i,
                    "time": third[0],
                    "type": "BEARISH FVG",
                })

    return zones


# ============================================================
# ORDER BLOCK
# ============================================================

def detect_order_block(
    candles,
    direction,
    start_index=0
):

    for i in range(
        len(candles) - 1,
        max(start_index, 0),
        -1
    ):

        candle = candles[i]

        if direction == "LONG" and is_bearish_candle(candle):

            return {
                "low": candle[3],
                "high": candle[2],
                "index": i,
                "time": candle[0],
                "type": "BULLISH ORDER BLOCK",
            }

        if direction == "SHORT" and is_bullish_candle(candle):

            return {
                "low": candle[3],
                "high": candle[2],
                "index": i,
                "time": candle[0],
                "type": "BEARISH ORDER BLOCK",
            }

    return None


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def premium_discount(candles):

    recent = candles[-50:]

    high = max(c[2] for c in recent)
    low = min(c[3] for c in recent)

    midpoint = (high + low) / 2
    price = candles[-1][4]

    if price > midpoint:
        zone = "PREMIUM"

    elif price < midpoint:
        zone = "DISCOUNT"

    else:
        zone = "EQUILIBRIUM"

    return {
        "high": high,
        "low": low,
        "midpoint": midpoint,
        "zone": zone,
    }


# ============================================================
# ENTRY ZONE
# ============================================================

def find_entry_zone(
    candles_5m,
    direction,
    mss
):

    if not mss:
        return None

    mss_time = mss["time"]

    start_index = next(
        (
            i for i, candle in enumerate(candles_5m)
            if candle[0] >= mss_time
        ),
        None
    )

    if start_index is None:
        return None

    if start_index >= len(candles_5m) - 1:
        return None

    zones = detect_fvgs(
        candles_5m,
        direction,
        start_index
    )

    if not zones:
        return None

    # Prefer the most recent FVG.
    fvg = zones[-1]

    ob = detect_order_block(
        candles_5m,
        direction,
        start_index
    )

    selected = fvg
    zone_type = fvg["type"]

    # Use overlap between FVG and OB when available.
    if ob:

        overlap_low = max(
            fvg["low"],
            ob["low"]
        )

        overlap_high = min(
            fvg["high"],
            ob["high"]
        )

        if overlap_low < overlap_high:

            selected = {
                "low": overlap_low,
                "high": overlap_high,
                "index": fvg["index"],
                "time": fvg["time"],
                "type": "FVG + ORDER BLOCK",
            }

            zone_type = "FVG + ORDER BLOCK"

    if (
        len(candles_5m) - 1 - selected["index"]
        > MAX_ENTRY_ZONE_AGE_5M
    ):
        return None

    return {
        "low": selected["low"],
        "high": selected["high"],
        "type": zone_type,
        "fvg": fvg,
        "order_block": ob,
        "index": selected["index"],
        "time": selected["time"],
    }


# ============================================================
# LIQUIDITY TARGET
# ============================================================

def find_liquidity_target(
    data,
    direction,
    entry
):

    h1 = data["1h"]

    if direction == "LONG":

        candidates = [
            x["price"]
            for x in h1["pivots_high"]
            if x["price"] > entry
        ]

        candidates += [
            x["price"]
            for x in h1["equal_highs"]
            if x["price"] > entry
        ]

        return min(candidates) if candidates else None

    candidates = [
        x["price"]
        for x in h1["pivots_low"]
        if x["price"] < entry
    ]

    candidates += [
        x["price"]
        for x in h1["equal_lows"]
        if x["price"] < entry
    ]

    return max(candidates) if candidates else None


# ============================================================
# TRADE PLAN
# ============================================================

def make_trade(
    symbol,
    price,
    data,
    direction,
    sweep,
    mss,
    zone
):

    if not all([sweep, mss, zone]):
        return None

    entry_low = zone["low"]
    entry_high = zone["high"]

    if entry_low <= 0 or entry_high <= entry_low:
        return None

    entry = (
        entry_low + entry_high
    ) / 2

    atr_value = data["15m"]["atr"]

    if not atr_value:
        return None

    buffer = atr_value * STOP_ATR_BUFFER

    if direction == "LONG":

        stop = sweep["wick"] - buffer

        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3

    else:

        stop = sweep["wick"] + buffer

        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

    stop_percent = (
        risk / entry
    ) * 100

    if not (
        MIN_STOP_PERCENT
        <= stop_percent
        <= MAX_STOP_PERCENT
    ):
        return None

    liquidity_target = find_liquidity_target(
        data,
        direction,
        entry
    )

    # TP2 is the minimum planned target.
    # A nearby liquidity pool may be used only
    # if it still provides at least 2R.

    if liquidity_target is not None:

        if direction == "LONG":

            if liquidity_target >= tp2:
                tp2 = min(tp2, liquidity_target)

        else:

            if liquidity_target <= tp2:
                tp2 = max(tp2, liquidity_target)

    rr2 = abs(tp2 - entry) / risk

    if rr2 < MIN_RR:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr1": 1.0,
        "rr2": rr2,
        "rr3": 3.0,
        "stop_percent": stop_percent,
        "leverage": MAX_LEVERAGE,
        "margin_mode": MARGIN_MODE,
        "sweep": sweep,
        "mss": mss,
        "zone": zone,
        "liquidity_target": liquidity_target,
        "risk_per_trade_percent": RISK_PER_TRADE_PERCENT,
        "current_price": price,
    }


# ============================================================
# PREMIUM / DISCOUNT FILTER
# ============================================================

def premium_discount_ok(
    candles,
    direction,
    price
):

    pd = premium_discount(candles)

    if direction == "LONG":

        return (
            pd["zone"] == "DISCOUNT",
            pd
        )

    return (
        pd["zone"] == "PREMIUM",
        pd
    )


# ============================================================
# MAIN SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(item):

    symbol = item["symbol"]
    lbank_price = item["price"]

    result = {
        "symbol": symbol,
        "status": "REJECTED",
        "reason": "",
        "price": lbank_price,
    }

    try:

        candles = {
            tf: get_candles(symbol, tf)
            for tf in INTERVALS
        }

        data = {
            tf: timeframe_analysis(candles[tf])
            for tf in INTERVALS
        }

        okx_price = data["5m"]["price"]

        price_difference = (
            abs(lbank_price - okx_price)
            / okx_price
        ) * 100

        if price_difference > PRICE_DIFF_LIMIT:

            result["reason"] = (
                f"LBank/OKX price difference "
                f"{price_difference:.2f}%"
            )

            return result

        bias = get_market_bias(data)

        if bias == "NEUTRAL":

            result.update({
                "status": "WATCH",
                "reason": "4H/1H trend alignment missing.",
                "data": data,
                "bias": bias,
            })

            return result

        direction = (
            "LONG"
            if bias == "BULLISH"
            else "SHORT"
        )

        # 15M liquidity sweep.
        sweep = detect_liquidity_sweep(
            candles["15m"],
            data["15m"],
            direction
        )

        if not sweep:

            result.update({
                "status": "WATCH",
                "direction": direction,
                "bias": bias,
                "reason": "No confirmed recent liquidity sweep.",
                "data": data,
            })

            return result

        # 15M structure confirmation.
        mss = detect_structure_shift(
            candles["15m"],
            data["15m"],
            sweep
        )

        if not mss:

            result.update({
                "status": "WATCH",
                "direction": direction,
                "bias": bias,
                "sweep": sweep,
                "reason": "Liquidity sweep detected; MSS not confirmed.",
                "data": data,
            })

            return result

        # 5M FVG / OB entry zone.
        zone = find_entry_zone(
            candles["5m"],
            direction,
            mss
        )

        if not zone:

            result.update({
                "status": "WATCH",
                "direction": direction,
                "bias": bias,
                "sweep": sweep,
                "mss": mss,
                "reason": "MSS confirmed; valid 5M FVG/OB zone unavailable.",
                "data": data,
            })

            return result

        # Premium / Discount.
        pd_ok, pd_data = premium_discount_ok(
            candles["1h"],
            direction,
            lbank_price
        )

        # Momentum confirmation.
        h1 = data["1h"]
        m15 = data["15m"]
        m5 = data["5m"]

        if direction == "LONG":

            momentum_ok = (
                h1["macd_hist"] is not None
                and h1["macd_hist"] > 0
                and h1["rsi"] is not None
                and h1["rsi"] > 45
            )

            volume_ok = (
                m5["volume_ratio"] is not None
                and m5["volume_ratio"] >= MIN_5M_VOLUME_RATIO
                and m15["volume_ratio"] is not None
                and m15["volume_ratio"] >= MIN_15M_VOLUME_RATIO
            )

        else:

            momentum_ok = (
                h1["macd_hist"] is not None
                and h1["macd_hist"] < 0
                and h1["rsi"] is not None
                and h1["rsi"] < 55
            )

            volume_ok = (
                m5["volume_ratio"] is not None
                and m5["volume_ratio"] >= MIN_5M_VOLUME_RATIO
                and m15["volume_ratio"] is not None
                and m15["volume_ratio"] >= MIN_15M_VOLUME_RATIO
            )

        # Entry is valid only if current LBank price
        # is inside the 5M entry zone.
        price_in_zone = (
            zone["low"] <= lbank_price <= zone["high"]
        )

        score = 40

        score += 20  # HTF bias
        score += 20  # Sweep + MSS

        if pd_ok:
            score += 10

        if momentum_ok:
            score += 5

        if volume_ok:
            score += 5

        score = min(100, score)

        result.update({
            "direction": direction,
            "bias": bias,
            "score": score,
            "data": data,
            "sweep": sweep,
            "mss": mss,
            "zone": zone,
            "premium_discount": pd_data,
            "price_in_zone": price_in_zone,
            "momentum_ok": momentum_ok,
            "volume_ok": volume_ok,
            "price_difference": price_difference,
        })

        if not pd_ok:

            result["status"] = "WATCH"

            result["reason"] = (
                f"Price is not in preferred {direction} "
                "Premium/Discount location."
            )

            return result

        if not momentum_ok:

            result["status"] = "WATCH"

            result["reason"] = (
                "MACD/RSI momentum confirmation missing."
            )

            return result

        if not volume_ok:

            result["status"] = "WATCH"

            result["reason"] = (
                "Volume confirmation missing."
            )

            return result

        if not price_in_zone:

            result["status"] = "WATCH"

            result["reason"] = (
                "Structure confirmed; waiting for price "
                "to return to the entry zone."
            )

            return result

        trade = make_trade(
            symbol,
            lbank_price,
            data,
            direction,
            sweep,
            mss,
            zone
        )

        if not trade:

            result["status"] = "WATCH"

            result["reason"] = (
                "Trade plan failed risk or minimum R:R validation."
            )

            return result

        if score < MIN_SIGNAL_SCORE:

            result["status"] = "WATCH"

            result["reason"] = (
                "Final setup score below signal threshold."
            )

            return result

        result["status"] = "SIGNAL"
        result["trade"] = trade
        result["reason"] = "Liquidity + MSS + entry confirmation."

        return result

    except SkipSymbol as exc:

        result["status"] = "REJECTED"
        result["reason"] = str(exc)

        return result

    except Exception as exc:

        result["status"] = "ERROR"
        result["reason"] = str(exc)

        return result


# ============================================================
# REPORT HELPERS
# ============================================================

def trend_text(item):

    if item.get("bullish"):
        return "BULLISH"

    if item.get("bearish"):
        return "BEARISH"

    return "NEUTRAL"


def volume_text(item):

    value = number(item.get("volume_ratio"))

    if value is None:
        return "N/A"

    return f"{value:.2f}x"


def rsi_text(item):

    value = number(item.get("rsi"))

    if value is None:
        return "N/A"

    return f"{value:.1f}"


def format_trade(trade):

    return "\n".join([
        f"Direction: {trade['direction']}",
        (
            f"Entry Zone: {fmt(trade['entry_low'])} - "
            f"{fmt(trade['entry_high'])}"
        ),
        f"Entry Midpoint: {fmt(trade['entry'])}",
        f"Stop Loss: {fmt(trade['stop'])}",
        f"TP1: {fmt(trade['tp1'])} | 1R",
        f"TP2: {fmt(trade['tp2'])} | {trade['rr2']:.2f}R",
        f"TP3: {fmt(trade['tp3'])} | 3R",
        f"Stop distance: {trade['stop_percent']:.2f}%",
        f"Leverage: {trade['leverage']}x {trade['margin_mode']}",
        (
            "Maximum planned account risk: "
            f"{trade['risk_per_trade_percent']:.2f}%"
        ),
    ])


def format_market_status(item):

    symbol = item.get("symbol", "UNKNOWN")

    status = item.get("status", "UNKNOWN")

    direction = item.get("direction", "-")

    score = number(item.get("score"), 0)

    lines = [
        f"• {symbol} | {status}",
        f"Price: {fmt(item.get('price'))}",
        f"Direction: {direction} | Score: {score:.1f}/100",
        f"HTF Bias: {item.get('bias', 'N/A')}",
    ]

    reason = item.get("reason")

    if reason:
        lines.append(f"Reason: {reason}")

    data = item.get("data") or {}

    if data:

        h4 = data.get("4h", {})
        h1 = data.get("1h", {})
        m15 = data.get("15m", {})
        m5 = data.get("5m", {})

        lines.extend([
            (
                f"Trend 4H/1H: "
                f"{trend_text(h4)} / {trend_text(h1)}"
            ),
            (
                f"RSI 4H/1H: "
                f"{rsi_text(h4)} / {rsi_text(h1)}"
            ),
            (
                f"Volume 15M/5M: "
                f"{volume_text(m15)} / {volume_text(m5)}"
            ),
            (
                f"1H Support/Resistance: "
                f"{fmt(h1.get('support'))} / "
                f"{fmt(h1.get('resistance'))}"
            ),
        ])

        pd_data = item.get("premium_discount")

        if pd_data:
            lines.append(
                f"Premium/Discount: {pd_data['zone']}"
            )

    sweep = item.get("sweep")

    if sweep:

        lines.append(
            f"Liquidity: {sweep['type']} "
            f"at {fmt(sweep['level'])}"
        )

    mss = item.get("mss")

    if mss:

        lines.append(
            f"Structure: {mss['type']} "
            f"at {fmt(mss['level'])}"
        )

    zone = item.get("zone")

    if zone:

        lines.append(
            f"Entry Zone ({zone['type']}): "
            f"{fmt(zone['low'])} - {fmt(zone['high'])}"
        )

    if status == "SIGNAL" and item.get("trade"):

        lines.append(
            format_trade(item["trade"])
        )

    else:

        lines.append("NO ENTRY — WAIT FOR CONFIRMATION.")

    return "\n".join(lines)


# ============================================================
# COMPLETE REPORT
# ============================================================

def build_report(
    results,
    total_symbols,
    missing_symbols
):

    results = results or []
    missing_symbols = missing_symbols or []

    result_map = {
        item.get("symbol"): item
        for item in results
        if isinstance(item, dict)
        and item.get("symbol")
    }

    signals = [
        x for x in results
        if x.get("status") == "SIGNAL"
    ]

    watches = [
        x for x in results
        if x.get("status") == "WATCH"
    ]

    lines = [
        "LBANK ICT/SMC LIQUIDITY SCANNER",
        "4H / 1H / 15M / 5M",
        "=" * 40,
        f"Core symbols: {len(CORE_SYMBOLS)}",
        f"Valid snapshots: {total_symbols}",
        f"Confirmed signals: {len(signals)}",
        f"Watch candidates: {len(watches)}",
        "",
        "ALL SYMBOLS",
        "-" * 40,
    ]

    for symbol in CORE_SYMBOLS:

        item = result_map.get(symbol)

        if item:

            lines.append(
                format_market_status(item)
            )

        else:

            lines.append(
                f"{symbol}: NO DATA — NO ENTRY"
            )

        lines.append("")

    lines.extend([
        "=" * 40,
        "CONFIRMED SIGNALS",
        "-" * 40,
    ])

    if signals:

        for item in signals:

            lines.append(
                format_market_status(item)
            )

            lines.append("")

    else:

        lines.append(
            "NO CONFIRMED SETUP — NO ENTRY."
        )

    lines.extend([
        "",
        "WATCHLIST",
        "-" * 40,
    ])

    if watches:

        for item in watches:

            lines.append(
                f"{item.get('symbol')}: "
                f"{item.get('direction', '-')}"
            )

            lines.append(
                f"Reason: {item.get('reason', '-')}"
            )

            lines.append(
                "WATCH ONLY — DO NOT ENTER."
            )

            lines.append("")

    else:

        lines.append("No watch candidates.")

    lines.extend([
        "=" * 40,
        "RISK MANAGEMENT",
        "-" * 40,
        "Maximum leverage: 2x.",
        "Margin mode: ISOLATED.",
        (
            "Maximum planned account risk per trade: "
            f"{RISK_PER_TRADE_PERCENT:.2f}%."
        ),
        "Position size must be calculated from stop distance.",
        "This scanner does NOT read account equity.",
        "This scanner does NOT place or manage orders.",
        "OHLCV liquidity zones are estimates, not actual order-book depth.",
        "Entry prices are indicative and may differ from executable bid/ask.",
        "Use exchange-side stop orders and account for fees/slippage.",
        "High leverage increases liquidation risk.",
        "Technical analysis is not a guarantee of profit.",
    ])

    if missing_symbols:

        lines.extend([
            "",
            "MISSING / STALE SNAPSHOTS",
            "-" * 40,
            ", ".join(missing_symbols),
        ])

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:

        print(
            "Telegram credentials missing. "
            "Report was not sent."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    max_length = 3900

    chunks = []
    remaining = str(message)

    while remaining:

        if len(remaining) <= max_length:

            chunks.append(remaining)
            break

        split_at = remaining.rfind(
            "\n",
            0,
            max_length
        )

        if split_at < max_length // 2:
            split_at = max_length

        chunks.append(
            remaining[:split_at]
        )

        remaining = remaining[split_at:].lstrip()

    success = True

    for index, chunk in enumerate(chunks, 1):

        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }

        try:

            response = SESSION.post(
                url,
                json=payload,
                timeout=20
            )

            if response.status_code != 200:

                success = False

                print(
                    f"Telegram error {index}: "
                    f"{response.status_code} "
                    f"{response.text[:300]}"
                )

            else:

                result = response.json()

                if not result.get("ok", False):

                    success = False

                    print(
                        f"Telegram rejected message {index}: "
                        f"{result}"
                    )

        except Exception as exc:

            success = False

            print(
                f"Telegram send error: {exc}"
            )

        time.sleep(0.5)

    return success


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("LBank ICT/SMC Liquidity Scanner starting...")
    print("=" * 60)

    snapshots = load_latest_lbank()

    if not isinstance(snapshots, dict):
        snapshots = {}

    missing_symbols = [
        symbol
        for symbol in CORE_SYMBOLS
        if symbol not in snapshots
    ]

    results = []

    print(
        f"Valid snapshots: "
        f"{len(snapshots)} / {len(CORE_SYMBOLS)}"
    )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                item
            ): symbol
            for symbol, item in snapshots.items()
        }

        for future in as_completed(futures):

            symbol = futures[future]

            try:

                result = future.result()

                if not isinstance(result, dict):

                    result = {
                        "symbol": symbol,
                        "status": "ERROR",
                        "reason": "Invalid analyzer result.",
                    }

                result.setdefault(
                    "symbol",
                    symbol
                )

                results.append(result)

                print(
                    f"{symbol}: "
                    f"{result.get('status', 'UNKNOWN')} | "
                    f"Score: {result.get('score', 0)}"
                )

            except Exception as exc:

                print(
                    f"{symbol} analysis error: {exc}"
                )

                results.append({
                    "symbol": symbol,
                    "status": "ERROR",
                    "reason": str(exc),
                })

    report = build_report(
        results=results,
        total_symbols=len(snapshots),
        missing_symbols=missing_symbols,
    )

    REPORT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print("\n" + report)

    sent = send_telegram(report)

    if sent:

        print(
            "\nTelegram report sent successfully."
        )

    else:

        print(
            "\nTelegram report could not be fully delivered."
        )

    print("\nScanner finished.")


if __name__ == "__main__":
    main()