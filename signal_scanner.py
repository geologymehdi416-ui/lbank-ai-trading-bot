#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
PRICE_DIFF_LIMIT = 0.75
MIN_TURNOVER = 100000

MAX_RETRIES = 3
MAX_WORKERS = 5

MAX_LEVERAGE = 2
RISK_PER_TRADE_PERCENT = 0.5

MIN_SIGNAL_SCORE = 75
MIN_WATCH_SCORE = 55

MIN_VOLUME_RATIO = 1.2
MIN_15M_VOLUME_RATIO = 0.8

MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0

PIVOT_LEFT = 3
PIVOT_RIGHT = 3

MAX_SWEEP_AGE = 8
MAX_MSS_AGE = 8

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
    "User-Agent": "LBank-Hybrid-ICT-SMC-Scanner/1.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class SkipSymbol(Exception):
    pass


# ============================================================
# HELPERS
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
    return "N/A" if value is None else f"{value:,.8g}"


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


def candle_interval_ms(timeframe):
    return {
        "5m": 300000,
        "15m": 900000,
        "1h": 3600000,
        "4h": 14400000,
    }[timeframe]


# ============================================================
# TECHNICAL INDICATORS
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

    return 100 - 100 / (1 + avg_gain / avg_loss)


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

    return line[-1], signal, line[-1] - signal


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

    with DATA_FILE.open("r", encoding="utf-8") as fh:
        for line in fh:
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
                    ["lastPrice", "price", "last", "close"]
                ))

                turnover = number(get_first(
                    row,
                    [
                        "turnover24h", "turnover",
                        "quoteVolume", "quote_volume",
                        "volValue", "amount", "volume_usdt"
                    ]
                ))

                timestamp = parse_timestamp(get_first(
                    row,
                    ["timestamp", "ts", "time", "updatedAt"]
                ))

                if price is None or price <= 0:
                    continue

                if timestamp is None:
                    continue

                if (
                    symbol not in latest
                    or timestamp > latest[symbol]["timestamp"]
                ):
                    latest[symbol] = {
                        "symbol": symbol,
                        "price": price,
                        "turnover": turnover or 0,
                        "timestamp": timestamp,
                    }

            except (ValueError, TypeError, KeyError):
                continue

    now = time.time()
    valid = {}

    for symbol, item in latest.items():
        age = (now - item["timestamp"]) / 60

        if age > MAX_SNAPSHOT_AGE_MINUTES:
            continue

        if item["turnover"] < MIN_TURNOVER:
            continue

        valid[symbol] = item

    if not valid:
        raise RuntimeError(
            "No fresh and valid LBank snapshots found"
        )

    return valid


# ============================================================
# OKX CANDLE DATA
# ============================================================

def okx_instrument(symbol):
    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"


def fetch_candles(symbol, timeframe, limit=250):
    inst_id = okx_instrument(symbol)

    params = {
        "instId": inst_id,
        "bar": INTERVALS[timeframe],
        "limit": str(limit),
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = SESSION.get(
                OKX_URL,
                params=params,
                timeout=20,
            )

            response.raise_for_status()
            payload = response.json()

            if payload.get("code") != "0":
                raise RuntimeError(
                    payload.get("msg", "OKX API error")
                )

            raw = payload.get("data", [])

            if not raw:
                raise RuntimeError("No candle data")

            candles = []

            for row in reversed(raw):
                if len(row) < 6:
                    continue

                ts = number(row[0])
                op = number(row[1])
                high = number(row[2])
                low = number(row[3])
                close = number(row[4])
                volume = number(row[5])

                if None in (ts, op, high, low, close, volume):
                    continue

                candles.append([
                    int(ts),
                    op,
                    high,
                    low,
                    close,
                    volume,
                ])

            if len(candles) < 205:
                raise RuntimeError(
                    f"Insufficient candles: {len(candles)}"
                )

            # حذف کندل باز برای جلوگیری از سیگنال ناپایدار
            interval = candle_interval_ms(timeframe)
            now_ms = int(time.time() * 1000)

            candles = [
                c for c in candles
                if c[0] + interval <= now_ms
            ]

            if len(candles) < 205:
                raise RuntimeError(
                    "Not enough closed candles"
                )

            return candles

        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise

            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError("Candle fetch failed")


# ============================================================
# PIVOT DETECTION
# ============================================================

def find_pivots(candles):
    highs = []
    lows = []

    left = PIVOT_LEFT
    right = PIVOT_RIGHT

    for i in range(left, len(candles) - right):
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

        if high > max(left_highs + right_highs):
            highs.append({
                "index": i,
                "price": high,
                "confirmed_at": i + right,
            })

        if low < min(left_lows + right_lows):
            lows.append({
                "index": i,
                "price": low,
                "confirmed_at": i + right,
            })

    return highs, lows


# ============================================================
# EQUAL HIGHS / EQUAL LOWS
# ============================================================

def find_equal_levels(levels, tolerance=0.001):
    result = []

    for i in range(len(levels)):
        for j in range(i + 1, len(levels)):
            a = levels[i]["price"]
            b = levels[j]["price"]

            if a <= 0 or b <= 0:
                continue

            difference = abs(a - b) / ((a + b) / 2)

            if difference <= tolerance:
                result.append({
                    "price": (a + b) / 2,
                    "first_index": levels[i]["index"],
                    "second_index": levels[j]["index"],
                })

    return result


# ============================================================
# LIQUIDITY MAP
# ============================================================

def liquidity_map(candles):
    highs, lows = find_pivots(candles)

    equal_highs = find_equal_levels(highs)
    equal_lows = find_equal_levels(lows)

    return {
        "highs": highs,
        "lows": lows,
        "equal_highs": equal_highs,
        "equal_lows": equal_lows,
    }
# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles, liquidity):
    highs = liquidity["highs"]
    lows = liquidity["lows"]

    if len(candles) < 20:
        return None

    last_index = len(candles) - 1

    # فقط آخرین 8 کندل بسته‌شده بررسی می‌شوند
    start = max(0, last_index - MAX_SWEEP_AGE)

    # Bullish sweep: شکست کف و برگشت بالای آن
    for pivot in reversed(lows):
        if pivot["confirmed_at"] >= start:
            continue

        level = pivot["price"]

        for i in range(last_index, start - 1, -1):
            candle = candles[i]

            if candle[3] < level and candle[4] > level:
                return {
                    "direction": "LONG",
                    "level": level,
                    "index": i,
                    "time": candle[0],
                    "extreme": candle[3],
                }

    # Bearish sweep: شکست سقف و برگشت زیر آن
    for pivot in reversed(highs):
        if pivot["confirmed_at"] >= start:
            continue

        level = pivot["price"]

        for i in range(last_index, start - 1, -1):
            candle = candles[i]

            if candle[2] > level and candle[4] < level:
                return {
                    "direction": "SHORT",
                    "level": level,
                    "index": i,
                    "time": candle[0],
                    "extreme": candle[2],
                }

    return None


# ============================================================
# MARKET STRUCTURE SHIFT
# ============================================================

def detect_mss(candles, liquidity, sweep):
    if not sweep:
        return None

    direction = sweep["direction"]
    sweep_index = sweep["index"]

    highs = liquidity["highs"]
    lows = liquidity["lows"]

    start = sweep_index + 1
    end = min(
        len(candles),
        sweep_index + MAX_MSS_AGE + 1
    )

    if start >= end:
        return None

    if direction == "LONG":
        candidates = [
            p for p in highs
            if p["confirmed_at"] <= sweep_index
        ]

        if not candidates:
            return None

        pivot = candidates[-1]
        level = pivot["price"]

        for i in range(start, end):
            candle = candles[i]

            if candle[4] > level:
                return {
                    "direction": "LONG",
                    "level": level,
                    "index": i,
                    "time": candle[0],
                }

    elif direction == "SHORT":
        candidates = [
            p for p in lows
            if p["confirmed_at"] <= sweep_index
        ]

        if not candidates:
            return None

        pivot = candidates[-1]
        level = pivot["price"]

        for i in range(start, end):
            candle = candles[i]

            if candle[4] < level:
                return {
                    "direction": "SHORT",
                    "level": level,
                    "index": i,
                    "time": candle[0],
                }

    return None


# ============================================================
# FAIR VALUE GAP
# ============================================================

def find_fvg(candles, direction, start_index=0):
    if len(candles) < 3:
        return None

    start = max(2, start_index)

    for i in range(len(candles) - 1, start - 1, -1):
        first = candles[i - 2]
        third = candles[i]

        if direction == "LONG":
            # Bullish FVG
            if third[3] > first[2]:
                return {
                    "type": "BULLISH FVG",
                    "low": first[2],
                    "high": third[3],
                    "index": i,
                    "time": third[0],
                }

        elif direction == "SHORT":
            # Bearish FVG
            if third[2] < first[3]:
                return {
                    "type": "BEARISH FVG",
                    "low": third[2],
                    "high": first[3],
                    "index": i,
                    "time": third[0],
                }

    return None


# ============================================================
# ORDER BLOCK
# ============================================================

def find_order_block(candles, fvg, direction):
    """
    Heuristic Order Block:
    آخرین کندل مخالف قبل از تشکیل FVG.
    """

    if not fvg:
        return None

    fvg_index = fvg["index"]
    start = max(0, fvg_index - 12)

    for i in range(fvg_index - 1, start - 1, -1):
        candle = candles[i]

        bullish_candle = candle[4] > candle[1]
        bearish_candle = candle[4] < candle[1]

        if direction == "LONG" and bearish_candle:
            return {
                "type": "BULLISH OB",
                "low": candle[3],
                "high": candle[2],
                "index": i,
                "time": candle[0],
            }

        if direction == "SHORT" and bullish_candle:
            return {
                "type": "BEARISH OB",
                "low": candle[3],
                "high": candle[2],
                "index": i,
                "time": candle[0],
            }

    return None


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def premium_discount(candles, price):
    recent = candles[-50:]

    swing_high = max(c[2] for c in recent)
    swing_low = min(c[3] for c in recent)

    midpoint = (swing_high + swing_low) / 2

    if price < midpoint:
        zone = "DISCOUNT"
    elif price > midpoint:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    return {
        "high": swing_high,
        "low": swing_low,
        "midpoint": midpoint,
        "zone": zone,
    }


# ============================================================
# ENTRY ZONE
# ============================================================

def find_entry_zone(
    candles,
    direction,
    mss,
    price,
    atr_value
):
    if not mss:
        return None

    fvg = find_fvg(
        candles,
        direction,
        start_index=mss["index"]
    )

    if not fvg:
        return None

    ob = find_order_block(
        candles,
        fvg,
        direction
    )

    zone_low = fvg["low"]
    zone_high = fvg["high"]

    # اگر OB با FVG همپوشانی داشته باشد، ناحیه ترکیبی می‌شود
    if ob:
        overlap_low = max(zone_low, ob["low"])
        overlap_high = min(zone_high, ob["high"])

        if overlap_low < overlap_high:
            zone_low = overlap_low
            zone_high = overlap_high

    if zone_high <= zone_low:
        return None

    # قیمت باید داخل ناحیه ورود باشد
    if not zone_low <= price <= zone_high:
        return None

    if atr_value is None or atr_value <= 0:
        return None

    return {
        "low": zone_low,
        "high": zone_high,
        "fvg": fvg,
        "ob": ob,
        "mid": (zone_low + zone_high) / 2,
    }


# ============================================================
# LIQUIDITY TARGET
# ============================================================

def find_liquidity_target(candles, direction, entry):
    highs, lows = find_pivots(candles)

    if direction == "LONG":
        targets = [
            p["price"]
            for p in highs
            if p["price"] > entry
        ]

        if not targets:
            return None

        return min(targets)

    targets = [
        p["price"]
        for p in lows
        if p["price"] < entry
    ]

    if not targets:
        return None

    return max(targets)


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def timeframe_analysis(candles):
    closes = [c[4] for c in candles]

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]

    current = closes[-1]

    bullish = (
        ema20 is not None
        and ema50 is not None
        and ema200 is not None
        and current > ema20 > ema50 > ema200
    )

    bearish = (
        ema20 is not None
        and ema50 is not None
        and ema200 is not None
        and current < ema20 < ema50 < ema200
    )

    volumes = [c[5] for c in candles]

    average_volume = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21 else None
    )

    volume_ratio = (
        safe_div(volumes[-1], average_volume)
        if average_volume else None
    )

    macd_line, macd_signal, macd_hist = macd(closes)

    return {
        "candles": candles,
        "price": current,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "bullish": bullish,
        "bearish": bearish,
        "rsi": rsi(closes),
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "atr": atr(candles),
        "volume_ratio": volume_ratio,
    }


# ============================================================
# HIGHER TIMEFRAME BIAS
# ============================================================

def higher_timeframe_bias(data):
    h4 = data["4h"]
    h1 = data["1h"]

    if h4["bullish"] and h1["bullish"]:
        return "BULLISH"

    if h4["bearish"] and h1["bearish"]:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# ICT / SMC SETUP DETECTION
# ============================================================

def detect_setup(data, price):
    candles = data["15m"]["candles"]

    liquidity = liquidity_map(candles)

    sweep = detect_liquidity_sweep(
        candles,
        liquidity
    )

    if not sweep:
        return None

    mss = detect_mss(
        candles,
        liquidity,
        sweep
    )

    if not mss:
        return None

    direction = sweep["direction"]

    zone = find_entry_zone(
        data["5m"]["candles"],
        direction,
        mss,
        price,
        data["5m"]["atr"]
    )

    if not zone:
        return None

    return {
        "direction": direction,
        "sweep": sweep,
        "mss": mss,
        "zone": zone,
        "liquidity": liquidity,
    }
# ============================================================
# SETUP SCORING
# ============================================================

def score_setup(data, direction, setup, pd):
    score = 0
    reasons = []

    h4 = data["4h"]
    h1 = data["1h"]
    m15 = data["15m"]
    m5 = data["5m"]

    is_long = direction == "LONG"

    # Higher timeframe trend
    if h4["bullish"] if is_long else h4["bearish"]:
        score += 15
        reasons.append("4H trend aligned")

    if h1["bullish"] if is_long else h1["bearish"]:
        score += 15
        reasons.append("1H trend aligned")

    # Liquidity sweep and MSS
    score += 15
    reasons.append("Liquidity sweep confirmed")

    score += 15
    reasons.append("MSS confirmed")

    # FVG
    if setup["zone"].get("fvg"):
        score += 10
        reasons.append("FVG identified")

    # Order Block
    if setup["zone"].get("ob"):
        score += 5
        reasons.append("Order Block identified")

    # Premium / Discount
    if is_long and pd["zone"] == "DISCOUNT":
        score += 10
        reasons.append("Discount zone")

    elif not is_long and pd["zone"] == "PREMIUM":
        score += 10
        reasons.append("Premium zone")

    # MACD
    for item in (h4, h1, m15, m5):
        hist = item["macd_hist"]

        if hist is None:
            continue

        if is_long and hist > 0:
            score += 2.5

        elif not is_long and hist < 0:
            score += 2.5

    # RSI
    rsi_value = h1["rsi"]

    if rsi_value is not None:
        if is_long and 45 <= rsi_value <= 70:
            score += 5

        elif not is_long and 30 <= rsi_value <= 55:
            score += 5

    # Volume
    volume5 = m5["volume_ratio"]
    volume15 = m15["volume_ratio"]

    if volume5 is not None and volume5 >= MIN_VOLUME_RATIO:
        score += 5
        reasons.append("5M volume confirmed")

    if (
        volume15 is not None
        and volume15 >= MIN_15M_VOLUME_RATIO
    ):
        score += 5
        reasons.append("15M volume acceptable")

    return min(round(score, 1), 100), reasons


# ============================================================
# TRADE PLAN
# ============================================================

def build_trade(
    direction,
    price,
    zone,
    atr_value,
    liquidity_target
):
    if atr_value is None or atr_value <= 0:
        return None

    entry = zone["mid"]

    if direction == "LONG":
        stop = min(
            zone["low"],
            price - atr_value * 1.5
        ) - atr_value * 0.15

        risk = entry - stop

    else:
        stop = max(
            zone["high"],
            price + atr_value * 1.5
        ) + atr_value * 0.15

        risk = stop - entry

    if risk <= 0 or entry <= 0:
        return None

    risk_percent = risk / entry * 100

    if not (
        MIN_RISK_PERCENT
        <= risk_percent
        <= MAX_RISK_PERCENT
    ):
        return None

    if direction == "LONG":
        tp1 = entry + risk
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3

        if liquidity_target is None:
            return None

        # تارگت نقدینگی باید حداقل 2R فاصله داشته باشد
        if liquidity_target < tp2:
            return None

        tp3 = min(tp3, liquidity_target)

        if tp3 <= tp2:
            return None

    else:
        tp1 = entry - risk
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

        if liquidity_target is None:
            return None

        if liquidity_target > tp2:
            return None

        tp3 = max(tp3, liquidity_target)

        if tp3 >= tp2:
            return None

    return {
        "direction": direction,
        "entry": entry,
        "zone_low": zone["low"],
        "zone_high": zone["high"],
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "liquidity_target": liquidity_target,
        "risk_percent": risk_percent,
        "leverage": MAX_LEVERAGE,
        "risk_reward": 3.0,
    }


# ============================================================
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(snapshot):
    symbol = snapshot["symbol"]
    price = snapshot["price"]

    if symbol in BLOCKED_SYMBOLS:
        raise SkipSymbol("Blocked symbol")

    try:
        age = (time.time() - snapshot["timestamp"]) / 60

        if age > MAX_SNAPSHOT_AGE_MINUTES:
            raise SkipSymbol("Stale LBank snapshot")

        data = {}

        # دریافت داده چهار تایم‌فریم
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(
                    fetch_candles,
                    symbol,
                    timeframe
                ): timeframe
                for timeframe in ("5m", "15m", "1h", "4h")
            }

            for future in as_completed(futures):
                timeframe = futures[future]
                candles = future.result()

                data[timeframe] = timeframe_analysis(candles)

        # بررسی اختلاف قیمت LBank و OKX
        okx_price = data["5m"]["price"]

        price_diff = (
            abs(price - okx_price)
            / okx_price
            * 100
        )

        if price_diff > PRICE_DIFF_LIMIT:
            raise SkipSymbol(
                f"Price difference too high: {price_diff:.2f}%"
            )

        # بررسی ساختار تایم‌فریم بالاتر
        bias = higher_timeframe_bias(data)

        if bias == "NEUTRAL":
            return {
                "symbol": symbol,
                "price": price,
                "status": "NO TRADE",
                "score": 0,
                "reason": "4H and 1H trend not aligned",
                "bias": bias,
            }

        direction = (
            "LONG" if bias == "BULLISH" else "SHORT"
        )

        # Premium / Discount
        pd = premium_discount(
            data["1h"]["candles"],
            price
        )

        # تشخیص Setup
        setup = detect_setup(data, price)

        if not setup:
            return {
                "symbol": symbol,
                "price": price,
                "status": "WATCH",
                "score": 0,
                "reason": "No confirmed sweep + MSS + entry zone",
                "bias": bias,
            }

        # جهت Setup باید با HTF هماهنگ باشد
        if setup["direction"] != direction:
            return {
                "symbol": symbol,
                "price": price,
                "status": "NO TRADE",
                "score": 0,
                "reason": "Setup conflicts with higher timeframe",
                "bias": bias,
            }

        score, reasons = score_setup(
            data,
            direction,
            setup,
            pd
        )

        if score < MIN_WATCH_SCORE:
            return {
                "symbol": symbol,
                "price": price,
                "status": "WATCH",
                "score": score,
                "reason": "Score below watch threshold",
                "bias": bias,
                "reasons": reasons,
            }

        # تعیین تارگت نقدینگی از 1H
        liquidity_target = find_liquidity_target(
            data["1h"]["candles"],
            direction,
            setup["zone"]["mid"]
        )

        trade = build_trade(
            direction,
            price,
            setup["zone"],
            data["5m"]["atr"],
            liquidity_target
        )

        if not trade:
            return {
                "symbol": symbol,
                "price": price,
                "status": "WATCH",
                "score": score,
                "reason": "Risk or liquidity target conditions not met",
                "bias": bias,
                "reasons": reasons,
            }

        status = (
            "SIGNAL"
            if score >= MIN_SIGNAL_SCORE
            else "WATCH"
        )

        return {
            "symbol": symbol,
            "price": price,
            "status": status,
            "score": score,
            "bias": bias,
            "direction": direction,
            "trade": trade,
            "reasons": reasons,
            "setup": setup,
            "premium_discount": pd,
            "price_diff": price_diff,
        }

    except SkipSymbol:
        raise

    except Exception as exc:
        return {
            "symbol": symbol,
            "price": price,
            "status": "ERROR",
            "score": 0,
            "reason": str(exc)[:200],
        }


# ============================================================
# SCAN ALL SYMBOLS
# ============================================================

def scan_market():
    snapshots = load_latest_lbank()

    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(analyze_symbol, snapshot): symbol
            for symbol, snapshot in snapshots.items()
        }

        for future in as_completed(futures):
            symbol = futures[future]

            try:
                result = future.result()
                results.append(result)

            except SkipSymbol as exc:
                results.append({
                    "symbol": symbol,
                    "status": "SKIPPED",
                    "score": 0,
                    "reason": str(exc),
                })

            except Exception as exc:
                results.append({
                    "symbol": symbol,
                    "status": "ERROR",
                    "score": 0,
                    "reason": str(exc)[:200],
                })

    results.sort(
        key=lambda x: x.get("score", 0),
        reverse=True
    )

    return results
https://api.telegram.org/bot{token}/sendMessage
