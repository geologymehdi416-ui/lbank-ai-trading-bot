#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SMART MARKET ANALYST V4

Market scanner:
- Futures candles: Bybit Linear USDT
- Futures reference price: LBank Futures
- Spot candles and price: LBank Spot
- No automatic order execution
"""

import argparse
import html
import json
import logging
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone


# =========================================================
# SETTINGS
# =========================================================

BOT_NAME = "SMART MARKET ANALYST V4"

MARKET_TYPE = os.getenv(
    "MARKET_TYPE", "FUTURES"
).strip().upper()

if MARKET_TYPE not in ("FUTURES", "SPOT"):
    raise ValueError("MARKET_TYPE must be FUTURES or SPOT")

DEFAULT_SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT",
    "BNB_USDT", "XRP_USDT", "DOGE_USDT",
    "ADA_USDT", "AVAX_USDT", "LINK_USDT",
    "DOT_USDT", "LTC_USDT", "ATOM_USDT",
    "NEAR_USDT", "BCH_USDT"
]

LBANK_BASE = os.getenv(
    "LBANK_BASE_URL",
    "https://api.lbkex.com"
).rstrip("/")

FUTURES_BASE = os.getenv(
    "FUTURES_BASE_URL",
    "https://lbkperp.lbank.com"
).rstrip("/")

BYBIT_BASE = os.getenv(
    "BYBIT_BASE_URL",
    "https://api.bybit.com"
).rstrip("/")

MIN_SCORE = 65.0
MIN_RR = 1.5
MAX_STOP_PCT = 3.0

RISK_PCT = 0.5
MAX_LEVERAGE = 2.0
FEE_PCT = 0.06

MIN_CANDLES = 100
CANDLE_LIMIT = 300
REQUEST_INTERVAL = 0.25

MSS_MIN_BODY = 0.55

MAX_CROSS_EXCHANGE_DIVERGENCE_PCT = 0.75
MAX_5M_CANDLE_AGE_SECONDS = 900

TIMEFRAMES = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("market_scanner")

_last_request = 0.0


# =========================================================
# EXCEPTIONS
# =========================================================

class ScanError(Exception):
    pass


class DataSourceError(ScanError):
    pass


# =========================================================
# GENERAL HELPERS
# =========================================================

def safe_float(value, default=None):

    try:
        result = float(value)

        if math.isfinite(result):
            return result

    except (TypeError, ValueError, OverflowError):
        pass

    return default


def clamp(value, low=0.0, high=100.0):

    return max(low, min(high, float(value)))


def validate_symbol(symbol):

    symbol = str(symbol).strip().upper()

    if not re.fullmatch(r"[A-Z0-9]+_[A-Z0-9]+", symbol):
        raise ScanError("Invalid symbol: " + symbol)

    return symbol


def normalize_market_symbol(symbol):

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(symbol).upper()
    )


def current_timestamp():

    return datetime.now(timezone.utc).isoformat()


def throttle():

    global _last_request

    elapsed = time.monotonic() - _last_request
    wait = REQUEST_INTERVAL - elapsed

    if wait > 0:
        time.sleep(wait)

    _last_request = time.monotonic()


# =========================================================
# CANDLE DATA
# =========================================================

class Candles:

    def __init__(self, rows):

        if not isinstance(rows, list):
            raise DataSourceError(
                "Candle rows must be a list"
            )

        if len(rows) < MIN_CANDLES:
            raise DataSourceError(
                f"Insufficient candles: {len(rows)}"
            )

        self.t = []
        self.o = []
        self.h = []
        self.l = []
        self.c = []
        self.v = []

        for row in rows:

            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise DataSourceError(
                    "Invalid candle row"
                )

            values = [
                safe_float(value)
                for value in row[:6]
            ]

            if any(value is None for value in values):
                raise DataSourceError(
                    "Invalid candle value"
                )

            ts, op, hi, lo, cl, vol = values

            if min(op, hi, lo, cl) <= 0:
                raise DataSourceError(
                    "Invalid candle price"
                )

            if hi < max(op, cl) or lo > min(op, cl):
                raise DataSourceError(
                    "Invalid candle OHLC"
                )

            if hi < lo or vol < 0:
                raise DataSourceError(
                    "Invalid candle range/volume"
                )

            if self.t and ts <= self.t[-1]:
                raise DataSourceError(
                    "Candle timestamps not increasing"
                )

            self.t.append(ts)
            self.o.append(op)
            self.h.append(hi)
            self.l.append(lo)
            self.c.append(cl)
            self.v.append(vol)

    def __len__(self):
        return len(self.c)

    def last_price(self):
        return self.c[-1]

    def is_valid(self):

        return (
            len(self) >= MIN_CANDLES
            and all(math.isfinite(x) for x in self.c)
        )


# =========================================================
# HTTP REQUEST
# =========================================================

def http_json(url, params=None, timeout=15, retries=3):

    last_error = None

    for attempt in range(retries):

        try:

            throttle()

            request_url = url

            if params:
                request_url += (
                    ("&" if "?" in request_url else "?")
                    + urllib.parse.urlencode(params)
                )

            request = urllib.request.Request(
                request_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "SmartMarketAnalyst/4.0"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout
            ) as response:

                raw = response.read().decode(
                    "utf-8", "replace"
                )

            result = json.loads(raw)

            if not isinstance(result, (dict, list)):
                raise DataSourceError(
                    "Unexpected API response"
                )

            return result

        except Exception as exc:

            last_error = str(exc)

            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise DataSourceError(
        f"HTTP request failed: {last_error}"
    )


# =========================================================
# SPOT DATA - LBANK
# =========================================================

def fetch_spot_klines(symbol, timeframe, limit=300):

    if timeframe not in TIMEFRAMES:
        raise ScanError("Unsupported timeframe")

    interval_map = {
        "5m": "minute5",
        "15m": "minute15",
        "1h": "hour1",
        "4h": "hour4"
    }

    now = int(time.time())
    start = now - TIMEFRAMES[timeframe] * limit

    data = http_json(
        LBANK_BASE + "/v2/kline.do",
        {
            "symbol": symbol.lower(),
            "size": limit,
            "type": interval_map[timeframe],
            "time": start
        }
    )

    if not isinstance(data, dict):
        raise DataSourceError(
            "Invalid Spot kline response"
        )

    if str(data.get("result", "")).lower() != "true":
        raise DataSourceError(
            "Spot kline API returned failure"
        )

    raw_rows = data.get("data")

    if not isinstance(raw_rows, list):
        raise DataSourceError(
            "Spot candle list missing"
        )

    rows_by_timestamp = {}

    for item in raw_rows:

        try:

            if isinstance(item, dict):

                ts = item.get(
                    "timestamp",
                    item.get("time")
                )

                op = item.get("open")
                hi = item.get("high")
                lo = item.get("low")
                cl = item.get("close")
                vol = item.get("volume")

            else:

                ts, op, hi, lo, cl, vol = item[:6]

            ts = safe_float(ts)

            if ts is None:
                continue

            if ts > 10**11:
                ts /= 1000

            if ts + TIMEFRAMES[timeframe] > time.time():
                continue

            rows_by_timestamp[ts] = [
                ts, op, hi, lo, cl, vol
            ]

        except (TypeError, ValueError, IndexError):
            continue

    rows = sorted(
        rows_by_timestamp.values(),
        key=lambda row: row[0]
    )

    return Candles(rows)


def fetch_spot_price(symbol):

    data = http_json(
        LBANK_BASE + "/v2/ticker/24hr.do",
        {"symbol": symbol.lower()}
    )

    items = (
        data.get("data", [])
        if isinstance(data, dict)
        else []
    )

    if not isinstance(items, list) or not items:
        raise DataSourceError(
            "Spot ticker unavailable"
        )

    ticker = items[0].get("ticker", {})
    price = safe_float(ticker.get("latest"))

    if price is None or price <= 0:
        raise DataSourceError(
            "Invalid Spot price"
        )

    return price


# =========================================================
# FUTURES DATA - BYBIT CANDLES
# =========================================================

def futures_adapter_configured():

    return True


def fetch_futures_klines(symbol, timeframe, limit=300):

    if timeframe not in TIMEFRAMES:
        raise ScanError("Unsupported timeframe")

    interval_map = {
        "5m": "5",
        "15m": "15",
        "1h": "60",
        "4h": "240"
    }

    bybit_symbol = normalize_market_symbol(symbol)

    if not bybit_symbol.endswith("USDT"):
        raise DataSourceError(
            f"Unsupported Bybit Linear symbol: {symbol}"
        )

    response = http_json(
        BYBIT_BASE + "/v5/market/kline",
        {
            "category": "linear",
            "symbol": bybit_symbol,
            "interval": interval_map[timeframe],
            "limit": min(int(limit), 1000)
        }
    )

    if not isinstance(response, dict):
        raise DataSourceError(
            "Invalid Bybit response"
        )

    if safe_float(response.get("retCode"), -1) != 0:
        raise DataSourceError(
            "Bybit API error: "
            + str(response.get("retMsg", "Unknown error"))
        )

    result = response.get("result")

    if not isinstance(result, dict):
        raise DataSourceError(
            "Bybit result object missing"
        )

    raw_rows = result.get("list")

    if not isinstance(raw_rows, list):
        raise DataSourceError(
            "Bybit kline list missing"
        )

    rows_by_timestamp = {}

    now = time.time()
    candle_seconds = TIMEFRAMES[timeframe]

    for item in raw_rows:

        try:

            if not isinstance(item, (list, tuple)):
                continue

            if len(item) < 6:
                continue

            # Bybit V5:
            # startTime, open, high, low, close, volume, turnover

            ts = safe_float(item[0])
            op = safe_float(item[1])
            hi = safe_float(item[2])
            lo = safe_float(item[3])
            cl = safe_float(item[4])
            vol = safe_float(item[5])

            values = (ts, op, hi, lo, cl, vol)

            if any(value is None for value in values):
                continue

            # Bybit timestamp is milliseconds.
            if ts > 10**11:
                ts /= 1000

            # Exclude unfinished candles.
            if ts + candle_seconds > now:
                continue

            rows_by_timestamp[ts] = [
                ts, op, hi, lo, cl, vol
            ]

        except (TypeError, ValueError, IndexError):
            continue

    rows = sorted(
        rows_by_timestamp.values(),
        key=lambda row: row[0]
    )

    return Candles(rows)


# =========================================================
# FUTURES REFERENCE PRICE - LBANK
# =========================================================

def fetch_futures_price(symbol):

    target = normalize_market_symbol(symbol)

    response = http_json(
        FUTURES_BASE + "/cfd/openApi/v1/pub/marketData",
        {
            "productGroup": "SwapU"
        }
    )

    if not isinstance(response, dict):
        raise DataSourceError(
            "Invalid LBank Futures response"
        )

    raw_items = response.get("data")

    if not isinstance(raw_items, list):
        raise DataSourceError(
            "LBank Futures marketData list missing"
        )

    for item in raw_items:

        if not isinstance(item, dict):
            continue

        item_symbol = normalize_market_symbol(
            item.get("symbol", "")
        )

        if item_symbol != target:
            continue

        price = safe_float(
            item.get("lastPrice")
        )

        if price is None or price <= 0:
            raise DataSourceError(
                f"Invalid LBank Futures price: {symbol}"
            )

        return price

    raise DataSourceError(
        f"LBank Futures symbol not found: {symbol}"
    )


# =========================================================
# MARKET DATA ROUTER
# =========================================================

def fetch_klines(symbol, timeframe, limit=300):

    symbol = validate_symbol(symbol)

    if MARKET_TYPE == "SPOT":
        return fetch_spot_klines(
            symbol, timeframe, limit
        )

    return fetch_futures_klines(
        symbol, timeframe, limit
    )


def fetch_price(symbol):

    symbol = validate_symbol(symbol)

    if MARKET_TYPE == "SPOT":
        return fetch_spot_price(symbol)

    return fetch_futures_price(symbol)
# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):

    if len(values) < period:
        return []

    alpha = 2.0 / (period + 1)
    output = [float(values[0])]

    for value in values[1:]:

        output.append(
            alpha * value + (1 - alpha) * output[-1]
        )

    return output


def rsi(values, period=14):

    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0

    rs = avg_gain / avg_loss

    return 100 - 100 / (1 + rs)


def macd(values, fast=12, slow=26, signal=9):

    fast_line = ema(values, fast)
    slow_line = ema(values, slow)

    if not fast_line or not slow_line:

        return {
            "line": 0.0,
            "signal": 0.0,
            "histogram": 0.0
        }

    line = [
        a - b
        for a, b in zip(fast_line, slow_line)
    ]

    signal_line = ema(line, signal)

    if not signal_line:

        return {
            "line": line[-1],
            "signal": 0.0,
            "histogram": line[-1]
        }

    return {
        "line": line[-1],
        "signal": signal_line[-1],
        "histogram": line[-1] - signal_line[-1]
    }


def atr(cs, period=14):

    if len(cs) < period + 1:
        raise ScanError(
            "Insufficient data for ATR"
        )

    true_ranges = []

    for i in range(1, len(cs)):

        true_ranges.append(
            max(
                cs.h[i] - cs.l[i],
                abs(cs.h[i] - cs.c[i - 1]),
                abs(cs.l[i] - cs.c[i - 1])
            )
        )

    value = sum(true_ranges[:period]) / period

    for item in true_ranges[period:]:

        value = (
            value * (period - 1) + item
        ) / period

    return value


def volume_ratio(cs):

    if len(cs) < 24:
        return 1.0

    base = sum(cs.v[-23:-3]) / 20
    recent = sum(cs.v[-3:]) / 3

    if base <= 0:
        return 1.0

    return recent / base


# =========================================================
# SWING STRUCTURE
# =========================================================

def find_swings(cs, depth=3):

    highs = []
    lows = []

    for i in range(depth, len(cs) - depth):

        is_high = (
            all(
                cs.h[i] > cs.h[i-k]
                for k in range(1, depth + 1)
            )
            and
            all(
                cs.h[i] >= cs.h[i+k]
                for k in range(1, depth + 1)
            )
        )

        is_low = (
            all(
                cs.l[i] < cs.l[i-k]
                for k in range(1, depth + 1)
            )
            and
            all(
                cs.l[i] <= cs.l[i+k]
                for k in range(1, depth + 1)
            )
        )

        if is_high:
            highs.append((i, cs.h[i]))

        if is_low:
            lows.append((i, cs.l[i]))

    return highs, lows


def structure_direction(highs, lows):

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


def detect_trend(cs, highs, lows):

    direction = structure_direction(
        highs, lows
    )

    price = cs.c[-1]

    if direction == 1:
        return "UP"

    if direction == -1:
        return "DOWN"

    e20 = ema(cs.c, 20)
    e50 = ema(cs.c, 50)

    if e20 and e50:

        if price > e20[-1] > e50[-1]:
            return "UP"

        if price < e20[-1] < e50[-1]:
            return "DOWN"

    return "RANGE"


# =========================================================
# TIMEFRAME ANALYSIS
# =========================================================

def build_tf(cs, name):

    if not cs.is_valid():
        raise DataSourceError(
            f"Invalid candle data: {name}"
        )

    highs, lows = find_swings(
        cs,
        2 if name == "4h" else 3
    )

    e20 = ema(cs.c, 20)
    e50 = ema(cs.c, 50)
    e200 = ema(cs.c, 200)

    macd_data = macd(cs.c)

    return {
        "name": name,
        "candles": cs,
        "close": cs.c[-1],
        "highs": highs,
        "lows": lows,
        "trend": detect_trend(
            cs, highs, lows
        ),
        "ema20": e20[-1] if e20 else None,
        "ema50": e50[-1] if e50 else None,
        "ema200": e200[-1] if e200 else None,
        "rsi": rsi(cs.c),
        "macd": macd_data["line"],
        "macd_signal": macd_data["signal"],
        "macd_hist": macd_data["histogram"],
        "atr": atr(cs),
        "volume_ratio": volume_ratio(cs)
    }


# =========================================================
# SUPPORT / RESISTANCE
# =========================================================

def liquidity_map(tf_data, price):

    highs = [
        value
        for _, value in tf_data["highs"]
        if value > price
    ]

    lows = [
        value
        for _, value in tf_data["lows"]
        if value < price
    ]

    return {
        "support": max(lows) if lows else None,
        "resistance": min(highs) if highs else None
    }


def market_regime(data):

    up = sum(
        1
        for tf in ("4h", "1h")
        if data[tf]["trend"] == "UP"
    )

    down = sum(
        1
        for tf in ("4h", "1h")
        if data[tf]["trend"] == "DOWN"
    )

    if up == 2:
        return "UPTREND"

    if down == 2:
        return "DOWNTREND"

    return "RANGE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_sweep(cs, side, lookback=25):

    start = max(2, len(cs) - lookback)

    for i in range(len(cs) - 1, start - 1, -1):

        if side == "LONG":

            level = min(cs.l[i-2:i])

            if cs.l[i] < level and cs.c[i] > level:

                return {
                    "level": level,
                    "extreme": cs.l[i],
                    "index": i
                }

        elif side == "SHORT":

            level = max(cs.h[i-2:i])

            if cs.h[i] > level and cs.c[i] < level:

                return {
                    "level": level,
                    "extreme": cs.h[i],
                    "index": i
                }

    return None


# =========================================================
# MARKET STRUCTURE SHIFT
# =========================================================

def detect_mss(cs, side, lookback=30):

    start = max(4, len(cs) - lookback)

    for i in range(len(cs) - 1, start - 1, -1):

        candle_range = cs.h[i] - cs.l[i]

        if candle_range <= 0:
            continue

        body = abs(cs.c[i] - cs.o[i])
        body_ratio = body / candle_range

        if body_ratio < MSS_MIN_BODY:
            continue

        if side == "LONG":

            level = max(cs.h[i-4:i])

            if (
                cs.c[i] > level
                and cs.c[i] > cs.o[i]
            ):

                return {
                    "level": level,
                    "index": i,
                    "close": cs.c[i]
                }

        else:

            level = min(cs.l[i-4:i])

            if (
                cs.c[i] < level
                and cs.c[i] < cs.o[i]
            ):

                return {
                    "level": level,
                    "index": i,
                    "close": cs.c[i]
                }

    return None


# =========================================================
# FAIR VALUE GAP
# =========================================================

def find_fvgs(cs, side, lookback=80):

    zones = []
    start = max(2, len(cs) - lookback)

    for i in range(start, len(cs)):

        if side == "LONG":

            low = cs.h[i-2]
            high = cs.l[i]

            if high <= low:
                continue

            # Ignore filled bullish gaps.
            if i + 1 < len(cs):

                if min(cs.l[i+1:]) <= low:
                    continue

        else:

            low = cs.h[i]
            high = cs.l[i-2]

            if high <= low:
                continue

            # Ignore filled bearish gaps.
            if i + 1 < len(cs):

                if max(cs.h[i+1:]) >= high:
                    continue

        zones.append({
            "low": low,
            "high": high,
            "index": i,
            "type": side,
            "source": "FVG"
        })

    return zones
# =========================================================
# ORDER BLOCK
# =========================================================

def find_obs(cs, side, lookback=60):

    zones = []
    start = max(1, len(cs) - lookback)

    for i in range(start, len(cs) - 1):

        if side == "LONG":

            opposing = cs.c[i] < cs.o[i]

            displacement = (
                cs.c[i+1] > cs.o[i+1]
                and cs.c[i+1] > cs.h[i]
            )

        else:

            opposing = cs.c[i] > cs.o[i]

            displacement = (
                cs.c[i+1] < cs.o[i+1]
                and cs.c[i+1] < cs.l[i]
            )

        if opposing and displacement:

            zones.append({
                "low": cs.l[i],
                "high": cs.h[i],
                "index": i,
                "type": side,
                "source": "OB"
            })

    return zones


# =========================================================
# ZONE SELECTION
# =========================================================

def pick_zone(data, side, price):

    candidates = []

    for tf in ("15m", "5m"):

        cs = data[tf]["candles"]

        zones = (
            find_fvgs(cs, side)
            + find_obs(cs, side)
        )

        for zone in zones:

            midpoint = (
                zone["low"] + zone["high"]
            ) / 2

            distance = abs(price - midpoint)

            if distance <= data[tf]["atr"] * 3:

                candidates.append(
                    (distance, zone)
                )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    return candidates[0][1]


# =========================================================
# TRADE PLAN
# =========================================================

def make_trade_plan(cs, side, price, atr_value):

    if price <= 0 or atr_value <= 0:
        return None

    highs, lows = find_swings(cs, 3)

    if side == "LONG":

        supports = [
            value
            for _, value in lows
            if value < price
        ]

        resistances = [
            value
            for _, value in highs
            if value > price
        ]

        stop_base = (
            max(supports)
            if supports
            else price - atr_value * 1.5
        )

        sl = stop_base - atr_value * 0.15

        risk = price - sl

        if risk <= 0:
            return None

        targets = sorted(resistances)

        tp1 = (
            targets[0]
            if targets
            else price + risk * 1.8
        )

        tp2 = (
            targets[1]
            if len(targets) > 1
            else price + risk * 2.5
        )

        if tp1 <= price:
            tp1 = price + risk * 1.5

        if tp2 <= tp1:
            tp2 = price + risk * 2.5

    elif side == "SHORT":

        resistances = [
            value
            for _, value in highs
            if value > price
        ]

        supports = [
            value
            for _, value in lows
            if value < price
        ]

        stop_base = (
            min(resistances)
            if resistances
            else price + atr_value * 1.5
        )

        sl = stop_base + atr_value * 0.15

        risk = sl - price

        if risk <= 0:
            return None

        targets = sorted(
            supports,
            reverse=True
        )

        tp1 = (
            targets[0]
            if targets
            else price - risk * 1.8
        )

        tp2 = (
            targets[1]
            if len(targets) > 1
            else price - risk * 2.5
        )

        if tp1 >= price:
            tp1 = price - risk * 1.5

        if tp2 >= tp1:
            tp2 = price - risk * 2.5

    else:
        return None

    stop_pct = abs(price - sl) / price * 100

    if stop_pct <= 0 or stop_pct > MAX_STOP_PCT:
        return None

    # FEE_PCT is the estimated fee per side in percent.
    fee_rate = FEE_PCT / 100

    reward = abs(tp2 - price)

    net_reward = (
        reward
        - (price + tp2) * fee_rate
    )

    net_risk = (
        abs(price - sl)
        + (price + sl) * fee_rate
    )

    if net_reward <= 0 or net_risk <= 0:
        return None

    rr = net_reward / net_risk

    if rr < MIN_RR:
        return None

    return {
        "Entry": price,
        "SL": sl,
        "TP1": tp1,
        "TP2": tp2,
        "Net_RR": round(rr, 2),
        "Risk_Pct": round(stop_pct, 3)
    }


# =========================================================
# SIDE EVALUATION
# =========================================================

def evaluate_side(data, side, price):

    result = {
        "side": side,
        "score": 0,
        "decision": "NO TRADE",
        "reasons": [],
        "blockers": [],
        "plan": None
    }

    direction = (
        "UP" if side == "LONG" else "DOWN"
    )

    opposite = (
        "DOWN" if side == "LONG" else "UP"
    )

    score = 0

    # -----------------------------------------------------
    # Higher timeframe trend
    # -----------------------------------------------------

    for tf, weight in (
        ("4h", 20),
        ("1h", 20)
    ):

        trend = data[tf]["trend"]

        if trend == direction:
            score += weight

        elif trend == "RANGE":
            score += weight * 0.35

    opposing = sum(
        1
        for tf in ("4h", "1h")
        if data[tf]["trend"] == opposite
    )

    if opposing == 2:

        result["blockers"].append(
            "روند هر دو تایم‌فریم بالاتر مخالف است"
        )

    # -----------------------------------------------------
    # EMA alignment
    # -----------------------------------------------------

    for tf, weight in (
        ("1h", 8),
        ("15m", 5)
    ):

        d = data[tf]

        e20 = d["ema20"]
        e50 = d["ema50"]

        if e20 is None or e50 is None:
            continue

        if (
            side == "LONG"
            and d["close"] > e20 > e50
        ):
            score += weight

        elif (
            side == "SHORT"
            and d["close"] < e20 < e50
        ):
            score += weight

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    r = data["15m"]["rsi"]

    if side == "LONG" and 50 <= r <= 68:
        score += 8

    elif side == "SHORT" and 32 <= r <= 50:
        score += 8

    # -----------------------------------------------------
    # MACD
    # -----------------------------------------------------

    d = data["15m"]

    if (
        side == "LONG"
        and d["macd"] > d["macd_signal"]
        and d["macd_hist"] > 0
    ):
        score += 7

    elif (
        side == "SHORT"
        and d["macd"] < d["macd_signal"]
        and d["macd_hist"] < 0
    ):
        score += 7

    # -----------------------------------------------------
    # Volume
    # -----------------------------------------------------

    volume = data["15m"]["volume_ratio"]

    if volume >= 1.5:
        score += 10

    elif volume >= 1.15:
        score += 7

    elif volume >= 0.9:
        score += 4

    else:
        score += 1

    # -----------------------------------------------------
    # Market structure
    # -----------------------------------------------------

    cs = data["15m"]["candles"]

    sweep = detect_sweep(cs, side)
    mss = detect_mss(cs, side)

    if sweep:

        score += 10

        result["reasons"].append(
            "Liquidity Sweep"
        )

    if mss:

        score += 12

        result["reasons"].append(
            "Market Structure Shift"
        )

    if not mss:

        result["blockers"].append(
            "تأیید MSS وجود ندارد"
        )

    # -----------------------------------------------------
    # FVG / Order Block
    # -----------------------------------------------------

    zone = pick_zone(
        data,
        side,
        price
    )

    if zone:

        score += 8

        result["reasons"].append(
            "ناحیه " + zone["source"] + " شناسایی شد"
        )

    # -----------------------------------------------------
    # Entry / Stop Loss / Targets
    # -----------------------------------------------------

    plan = make_trade_plan(
        cs,
        side,
        price,
        data["15m"]["atr"]
    )

    if not plan:

        result["blockers"].append(
            "طرح ورود، حد ضرر یا RR معتبر نیست"
        )

    else:

        if plan["Net_RR"] >= 2.5:
            score += 8

        elif plan["Net_RR"] >= MIN_RR:
            score += 5

    # -----------------------------------------------------
    # Final score
    # -----------------------------------------------------

    score -= opposing * 8

    result["score"] = round(
        clamp(score),
        1
    )

    result["sweep"] = sweep
    result["mss"] = mss
    result["zone"] = zone

    if (
        plan
        and not result["blockers"]
        and score >= MIN_SCORE
    ):

        result["decision"] = side
        result["plan"] = plan

    return result
# =========================================================
# SYMBOL ANALYSIS
# =========================================================

def analyze_symbol(symbol):

    symbol = validate_symbol(symbol)

    # Reference price:
    # Futures = LBank Futures
    # Spot = LBank Spot
    price = fetch_price(symbol)

    if price <= 0:
        raise DataSourceError(
            f"Invalid reference price: {symbol}"
        )

    data = {}

    # Higher timeframe first.
    for timeframe in (
        "4h",
        "1h",
        "15m",
        "5m"
    ):

        candles = fetch_klines(
            symbol,
            timeframe,
            CANDLE_LIMIT
        )

        data[timeframe] = build_tf(
            candles,
            timeframe
        )

    # -----------------------------------------------------
    # Cross-exchange validation
    # -----------------------------------------------------

    reference_close = data["5m"]["candles"].c[-1]
    candle_timestamp = data["5m"]["candles"].t[-1]

    candle_age = (
        time.time()
        - (
            candle_timestamp
            + TIMEFRAMES["5m"]
        )
    )

    divergence_pct = 0.0
    data_blocker = None

    if MARKET_TYPE == "FUTURES":

        divergence_pct = (
            abs(price - reference_close)
            / reference_close
            * 100
        )

        if candle_age < -30:

            data_blocker = (
                "زمان کندل ۵ دقیقه‌ای نامعتبر است"
            )

        elif candle_age > MAX_5M_CANDLE_AGE_SECONDS:

            data_blocker = (
                "کندل ۵ دقیقه‌ای Bybit قدیمی است"
            )

        elif (
            divergence_pct
            > MAX_CROSS_EXCHANGE_DIVERGENCE_PCT
        ):

            data_blocker = (
                "اختلاف قیمت Bybit و LBank بیش از حد مجاز است"
            )

    # -----------------------------------------------------
    # Evaluate LONG and SHORT independently
    # -----------------------------------------------------

    long_result = evaluate_side(
        data,
        "LONG",
        price
    )

    short_result = evaluate_side(
        data,
        "SHORT",
        price
    )

    # -----------------------------------------------------
    # Block both directions if data is unreliable
    # -----------------------------------------------------

    if data_blocker:

        for result in (
            long_result,
            short_result
        ):

            result["decision"] = "NO TRADE"
            result["plan"] = None

            result["blockers"].append(
                data_blocker
            )

    # -----------------------------------------------------
    # Select one non-contradictory signal
    # -----------------------------------------------------

    qualified = []

    if long_result["decision"] == "LONG":
        qualified.append(long_result)

    if short_result["decision"] == "SHORT":
        qualified.append(short_result)

    if len(qualified) == 1:

        signal = qualified[0]
        decision = signal["decision"]

    else:

        signal = None
        decision = "NO TRADE"

    # -----------------------------------------------------
    # Support and resistance
    # -----------------------------------------------------

    levels = liquidity_map(
        data["1h"],
        price
    )

    # -----------------------------------------------------
    # Source information
    # -----------------------------------------------------

    if MARKET_TYPE == "FUTURES":

        candle_source = (
            "Bybit Linear USDT Perpetual"
        )

        price_source = (
            "LBank Futures"
        )

    else:

        candle_source = "LBank Spot"
        price_source = "LBank Spot"

    return {
        "symbol": symbol,
        "price": price,
        "decision": decision,
        "signal": signal,
        "long": long_result,
        "short": short_result,
        "regime": market_regime(data),
        "support": levels["support"],
        "resistance": levels["resistance"],
        "candle_source": candle_source,
        "price_source": price_source,
        "reference_close": reference_close,
        "divergence_pct": round(
            divergence_pct,
            4
        ),
        "candle_age_seconds": round(
            candle_age,
            1
        ),
        "data_blocker": data_blocker,
        "timeframes": {
            tf: {
                "trend": data[tf]["trend"],
                "rsi": round(data[tf]["rsi"], 2),
                "ema20": data[tf]["ema20"],
                "ema50": data[tf]["ema50"],
                "ema200": data[tf]["ema200"],
                "macd": data[tf]["macd"],
                "macd_signal": data[tf]["macd_signal"],
                "macd_hist": data[tf]["macd_hist"],
                "volume_ratio": round(
                    data[tf]["volume_ratio"],
                    3
                )
            }
            for tf in ("4h", "1h", "15m", "5m")
        },
        "timestamp": current_timestamp()
    }


# =========================================================
# FORMATTING HELPERS
# =========================================================

def fmt_price(value):

    if value is None:
        return "-"

    value = safe_float(value)

    if value is None:
        return "-"

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return (
            f"{value:.5f}"
            .rstrip("0")
            .rstrip(".")
        )

    return (
        f"{value:.9f}"
        .rstrip("0")
        .rstrip(".")
    )


def fmt_indicator(value, digits=2):

    value = safe_float(value)

    if value is None:
        return "-"

    return f"{value:.{digits}f}"


# =========================================================
# TIMEFRAME REPORT
# =========================================================

def build_timeframe_lines(item):

    lines = []

    timeframes = item.get(
        "timeframes",
        {}
    )

    for tf in ("4h", "1h", "15m", "5m"):

        d = timeframes.get(tf)

        if not d:
            continue

        lines.append(
            f"{tf} | Trend: {d['trend']} "
            f"| RSI: {fmt_indicator(d['rsi'])} "
            f"| Vol: {fmt_indicator(d['volume_ratio'])}x"
        )

    return lines


# =========================================================
# SIGNAL CARD
# =========================================================

def build_card(item):

    symbol = html.escape(
        item["symbol"]
    )

    lines = [
        f"<b>{symbol}</b>",
        f"قیمت مرجع: <code>{fmt_price(item['price'])}</code>",
        f"رژیم بازار: {item['regime']}",
        f"حمایت: {fmt_price(item['support'])}",
        f"مقاومت: {fmt_price(item['resistance'])}",
        "",
        "<b>تحلیل تایم‌فریم‌ها</b>"
    ]

    lines.extend(
        build_timeframe_lines(item)
    )

    if MARKET_TYPE == "FUTURES":

        lines.extend([
            "",
            "<b>کنترل منابع داده</b>",
            f"منبع کندل: {item['candle_source']}",
            f"منبع قیمت: {item['price_source']}",
            (
                "بسته‌شدن آخرین کندل ۵m: "
                f"{fmt_price(item['reference_close'])}"
            ),
            (
                "اختلاف قیمت دو منبع: "
                f"{item['divergence_pct']}%"
            ),
            (
                "سن کندل ۵m: "
                f"{item['candle_age_seconds']} ثانیه"
            )
        ])

    signal = item["signal"]

    # -----------------------------------------------------
    # NO TRADE
    # -----------------------------------------------------

    if not signal:

        lines.extend([
            "",
            "<b>NO TRADE / ورود نکن</b>"
        ])

        best = max(
            (item["long"], item["short"]),
            key=lambda result: result["score"]
        )

        lines.append(
            f"جهت تحت نظر: {best['side']}"
        )

        lines.append(
            f"امتیاز: {best['score']}"
        )

        lines.append(
            "",
            "<b>دلایل عدم ورود</b>"
        )

        for reason in best["blockers"][:6]:

            lines.append(
                "• " + html.escape(reason)
            )

        other = (
            item["short"]
            if best["side"] == "LONG"
            else item["long"]
        )

        if other["blockers"]:

            lines.append(
                f"امتیاز جهت مقابل: {other['score']}"
            )

        return "\n".join(lines)

    # -----------------------------------------------------
    # VALID SIGNAL
    # -----------------------------------------------------

    plan = signal["plan"]

    lines.extend([
        "",
        f"سیگنال: <b>{signal['decision']}</b>",
        f"امتیاز: <b>{signal['score']}</b>",
        f"ورود: <code>{fmt_price(plan['Entry'])}</code>",
        f"حد ضرر: <code>{fmt_price(plan['SL'])}</code>",
        f"تارگت ۱: <code>{fmt_price(plan['TP1'])}</code>",
        f"تارگت ۲: <code>{fmt_price(plan['TP2'])}</code>",
        f"RR خالص تقریبی: {plan['Net_RR']}",
        f"ریسک فاصله حد ضرر: {plan['Risk_Pct']}%"
    ])

    for reason in signal["reasons"]:

        lines.append(
            "• " + html.escape(reason)
        )

    return "\n".join(lines)
# =========================================================
# FULL REPORT
# =========================================================

def format_report(results):

    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    longs = sum(
        1
        for item in results
        if item["decision"] == "LONG"
    )

    shorts = sum(
        1
        for item in results
        if item["decision"] == "SHORT"
    )

    no_trade = (
        len(results) - longs - shorts
    )

    lines = [
        f"<b>{BOT_NAME}</b>",
        f"Market: {MARKET_TYPE}",
        f"Time: {now}",
        "",
        f"Scanned: {len(results)}",
        f"LONG: {longs}",
        f"SHORT: {shorts}",
        f"NO TRADE: {no_trade}",
        "",
        "=============================="
    ]

    for item in results:

        lines.append(
            build_card(item)
        )

        lines.append(
            "=============================="
        )

    lines.extend([
        "",
        "<b>RISK MANAGEMENT</b>",
        f"Base risk: {RISK_PCT}%",
        f"Maximum configured leverage: {MAX_LEVERAGE}x",
        f"Maximum stop distance: {MAX_STOP_PCT}%",
        f"Minimum net RR: {MIN_RR}",
        "",
        "<b>DATA SOURCES</b>"
    ])

    if MARKET_TYPE == "FUTURES":

        lines.extend([
            "Candles: Bybit Linear USDT Perpetual",
            "Reference price: LBank Futures",
            (
                "Maximum cross-exchange divergence: "
                f"{MAX_CROSS_EXCHANGE_DIVERGENCE_PCT}%"
            ),
            (
                "Maximum 5m candle age: "
                f"{MAX_5M_CANDLE_AGE_SECONDS} seconds"
            )
        ])

    else:

        lines.extend([
            "Candles: LBank Spot",
            "Reference price: LBank Spot"
        ])

    lines.extend([
        "",
        "High leverage can cause rapid liquidation.",
        "Position sizing must be based on account risk.",
        "This program does not place orders.",
        "Signals are analytical estimates, not guarantees."
    ])

    return "\n".join(lines)


# =========================================================
# SYMBOL ARGUMENTS
# =========================================================

def parse_symbols(value):

    if not value:
        return DEFAULT_SYMBOLS.copy()

    result = []

    for raw_symbol in value.split(","):

        symbol = validate_symbol(raw_symbol)

        if symbol not in result:
            result.append(symbol)

    if not result:
        raise ScanError(
            "No valid symbols supplied"
        )

    return result


# =========================================================
# MAIN
# =========================================================

def main():

    parser = argparse.ArgumentParser(
        description=BOT_NAME
    )

    parser.add_argument(
        "--symbols",
        default="",
        help="BTC_USDT,ETH_USDT,SOL_USDT"
    )

    parser.add_argument(
        "--output",
        default="report.txt"
    )

    args = parser.parse_args()

    try:

        symbols = parse_symbols(
            args.symbols
        )

    except ScanError as exc:

        log.error(
            "Invalid symbol arguments: %s",
            exc
        )

        return 2

    log.info(
        "Starting %s",
        BOT_NAME
    )

    log.info(
        "Market type: %s",
        MARKET_TYPE
    )

    if MARKET_TYPE == "FUTURES":

        log.info(
            "Candle source: Bybit Linear"
        )

        log.info(
            "Reference price source: LBank Futures"
        )

    else:

        log.info(
            "Spot data source: LBank"
        )

    results = []
    failures = []

    # -----------------------------------------------------
    # Scan symbols
    # -----------------------------------------------------

    for symbol in symbols:

        try:

            log.info(
                "Scanning %s",
                symbol
            )

            result = analyze_symbol(
                symbol
            )

            results.append(result)

            log.info(
                "%s => %s | LONG %.1f | SHORT %.1f",
                symbol,
                result["decision"],
                result["long"]["score"],
                result["short"]["score"]
            )

        except Exception as exc:

            failures.append({
                "symbol": symbol,
                "error": str(exc)
            })

            log.exception(
                "Failed to analyze %s: %s",
                symbol,
                exc
            )

        time.sleep(
            REQUEST_INTERVAL
        )

    # -----------------------------------------------------
    # No successful symbols
    # -----------------------------------------------------

    if not results:

        message = (
            "NO TRADE — NO SYMBOLS WERE SUCCESSFULLY ANALYZED.\n"
            "Check API connectivity, symbol availability and response format."
        )

        log.error(message)

        try:

            with open(
                args.output,
                "w",
                encoding="utf-8"
            ) as file:

                file.write(message)

        except OSError:
            pass

        return 1

    # -----------------------------------------------------
    # Build report
    # -----------------------------------------------------

    report = format_report(
        results
    )

    if failures:

        report += "\n\n<b>SCAN ERRORS</b>\n"

        for failure in failures:

            report += (
                f"\n{html.escape(failure['symbol'])}: "
                f"{html.escape(failure['error'])}"
            )

    # -----------------------------------------------------
    # Save report
    # -----------------------------------------------------

    try:

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(report)

    except OSError as exc:

        log.error(
            "Cannot save report: %s",
            exc
        )

        return 1

    print(report)

    log.info(
        "Report saved to %s",
        args.output
    )

    log.info(
        "Completed: %d successful, %d failed",
        len(results),
        len(failures)
    )

    return 0


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    sys.exit(main())