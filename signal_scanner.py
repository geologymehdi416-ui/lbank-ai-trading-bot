
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Smart Market Analyst V3
Market scanner for LBank.
No automatic order execution.
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import urllib.error

from datetime import datetime, timezone
from typing import Any, Optional

# =========================================================
# SETTINGS
# =========================================================

BOT_NAME = "SMART MARKET ANALYST V3"

MARKET_TYPE = os.getenv("MARKET_TYPE", "FUTURES").upper()

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
SWING_DEPTH = 3

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
            raise DataSourceError("Candle rows must be a list")

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

            if len(row) < 6:
                raise DataSourceError("Invalid candle row")

            values = [safe_float(x) for x in row[:6]]

            if any(x is None for x in values):
                raise DataSourceError("Invalid candle value")

            ts, op, hi, lo, cl, vol = values

            if min(op, hi, lo, cl) <= 0:
                raise DataSourceError("Invalid candle price")

            if hi < max(op, cl) or lo > min(op, cl):
                raise DataSourceError("Invalid candle OHLC")

            if hi < lo or vol < 0:
                raise DataSourceError("Invalid candle range/volume")

            if self.t and ts <= self.t[-1]:
                raise DataSourceError("Candle timestamps not increasing")

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

            if params:
                url += (
                    ("&" if "?" in url else "?")
                    + urllib.parse.urlencode(params)
                )

            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "SmartMarketAnalyst/3.0"
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
                raise DataSourceError("Unexpected API response")

            return result

        except Exception as exc:

            last_error = str(exc)

            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise DataSourceError(
        f"HTTP request failed: {last_error}"
    )


# =========================================================
# SPOT DATA
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
        raise DataSourceError("Invalid Spot kline response")

    if str(data.get("result", "")).lower() != "true":
        raise DataSourceError("Spot kline API returned failure")

    raw_rows = data.get("data")

    if not isinstance(raw_rows, list):
        raise DataSourceError("Spot candle list missing")

    rows = []

    for item in raw_rows:

        try:
            if isinstance(item, dict):
                ts = item.get("timestamp", item.get("time"))
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

            rows.append([
                ts, op, hi, lo, cl, vol
            ])

        except (TypeError, ValueError, IndexError):
            continue

    rows.sort(key=lambda x: float(x[0]))

    return Candles(rows)


def fetch_spot_price(symbol):

    data = http_json(
        LBANK_BASE + "/v2/ticker/24hr.do",
        {"symbol": symbol.lower()}
    )

    items = data.get("data", []) if isinstance(data, dict) else []

    if not items:
        raise DataSourceError("Spot ticker unavailable")

    ticker = items[0].get("ticker", {})
    price = safe_float(ticker.get("latest"))

    if not price or price <= 0:
        raise DataSourceError("Invalid Spot price")

    return price


def fetch_spot_pairs():

    data = http_json(
        LBANK_BASE + "/v2/currencyPairs.do"
    )

    items = data.get("data", []) if isinstance(data, dict) else []

    if not isinstance(items, list):
        raise DataSourceError("Invalid Spot pair list")

    return {
        str(item).upper()
        for item in items
        if isinstance(item, str)
    }


# =========================================================
# FUTURES ADAPTER
# =========================================================

def futures_adapter_configured():

    return bool(
        os.getenv("FUTURES_KLINE_URL", "").strip()
        and os.getenv("FUTURES_PRICE_URL", "").strip()
        and os.getenv("FUTURES_OHLCV_FORMAT", "").strip()
    )


def _extract_futures_rows(response):

    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        raise DataSourceError("Invalid Futures response")

    for key in ("data", "rows", "result", "candles"):

        value = response.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for subkey in ("data", "rows", "candles"):
                if isinstance(value.get(subkey), list):
                    return value[subkey]

    raise DataSourceError(
        "Futures OHLCV array not found in response"
    )


def fetch_futures_klines(symbol, timeframe, limit=300):

    if not futures_adapter_configured():
        raise DataSourceError(
            "Futures adapter configuration is incomplete"
        )

    if timeframe not in TIMEFRAMES:
        raise ScanError("Unsupported timeframe")

    url = os.getenv("FUTURES_KLINE_URL").strip()

    fmt = os.getenv("FUTURES_OHLCV_FORMAT").lower().strip()

    if fmt not in ("list", "dict"):
        raise DataSourceError(
            "FUTURES_OHLCV_FORMAT must be list or dict"
        )

    params = {
        "symbol": symbol,
        "interval": timeframe,
        "limit": limit
    }

    response = http_json(url, params)
    raw_rows = _extract_futures_rows(response)

    rows = []

    for item in raw_rows:

        try:
            if fmt == "list":
                ts, op, hi, lo, cl, vol = item[:6]

            else:
                ts = item.get("timestamp", item.get("time"))
                op = item.get("open")
                hi = item.get("high")
                lo = item.get("low")
                cl = item.get("close")
                vol = item.get("volume")

            ts = safe_float(ts)

            if ts is None:
                continue

            if ts > 10**11:
                ts /= 1000

            if ts + TIMEFRAMES[timeframe] > time.time():
                continue

            rows.append([
                ts, op, hi, lo, cl, vol
            ])

        except (TypeError, ValueError, IndexError):
            continue

    rows.sort(key=lambda x: float(x[0]))

    return Candles(rows)


def fetch_futures_price(symbol):

    if not futures_adapter_configured():
        raise DataSourceError("Futures adapter is not configured")

    url = os.getenv("FUTURES_PRICE_URL").strip()

    response = http_json(url, {"symbol": symbol})

    if isinstance(response, dict):
        candidates = [
            response,
            response.get("data"),
            response.get("result")
        ]

        for item in candidates:

            if isinstance(item, dict):

                for key in (
                    "lastPrice",
                    "last_price",
                    "last",
                    "price"
                ):
                    price = safe_float(item.get(key))

                    if price and price > 0:
                        return price

    raise DataSourceError("Futures price not found")


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

    return {
        "line": line[-1],
        "signal": signal_line[-1],
        "histogram": line[-1] - signal_line[-1]
    }


def atr(cs, period=14):

    if len(cs) < period + 1:
        raise ScanError("Insufficient data for ATR")

    tr = []

    for i in range(1, len(cs)):
        tr.append(max(
            cs.h[i] - cs.l[i],
            abs(cs.h[i] - cs.c[i - 1]),
            abs(cs.l[i] - cs.c[i - 1])
        ))

    value = sum(tr[:period]) / period

    for item in tr[period:]:
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

        if (
            all(cs.h[i] > cs.h[i-k] for k in range(1, depth+1))
            and
            all(cs.h[i] >= cs.h[i+k] for k in range(1, depth+1))
        ):
            highs.append((i, cs.h[i]))

        if (
            all(cs.l[i] < cs.l[i-k] for k in range(1, depth+1))
            and
            all(cs.l[i] <= cs.l[i+k] for k in range(1, depth+1))
        ):
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

    direction = structure_direction(highs, lows)
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

    highs, lows = find_swings(
        cs, 2 if name == "4h" else 3
    )

    e20 = ema(cs.c, 20)
    e50 = ema(cs.c, 50)
    e200 = ema(cs.c, 200)

    m = macd(cs.c)

    return {
        "name": name,
        "candles": cs,
        "close": cs.c[-1],
        "highs": highs,
        "lows": lows,
        "trend": detect_trend(cs, highs, lows),
        "ema20": e20[-1] if e20 else None,
        "ema50": e50[-1] if e50 else None,
        "ema200": e200[-1] if e200 else None,
        "rsi": rsi(cs.c),
        "macd": m["line"],
        "macd_signal": m["signal"],
        "macd_hist": m["histogram"],
        "atr": atr(cs),
        "volume_ratio": volume_ratio(cs)
    }


# =========================================================
# SUPPORT / RESISTANCE
# =========================================================

def liquidity_map(tf_data, price):

    highs = [
        value for _, value in tf_data["highs"]
        if value > price
    ]

    lows = [
        value for _, value in tf_data["lows"]
        if value < price
    ]

    return {
        "support": max(lows) if lows else None,
        "resistance": min(highs) if highs else None
    }


def market_regime(data):

    up = sum(
        1 for tf in ("4h", "1h")
        if data[tf]["trend"] == "UP"
    )

    down = sum(
        1 for tf in ("4h", "1h")
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

            if cs.c[i] > level and cs.c[i] > cs.o[i]:
                return {
                    "level": level,
                    "index": i,
                    "close": cs.c[i]
                }

        else:

            level = min(cs.l[i-4:i])

            if cs.c[i] < level and cs.c[i] < cs.o[i]:
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

            if min(cs.l[i+1:]) <= low if i + 1 < len(cs) else False:
                continue

        else:

            low = cs.h[i]
            high = cs.l[i-2]

            if high <= low:
                continue

            if max(cs.h[i+1:]) >= high if i + 1 < len(cs) else False:
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
                candidates.append((distance, zone))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])

    return candidates[0][1]


# =========================================================
# TRADE PLAN
# =========================================================

def make_trade_plan(cs, side, price, atr_value):

    highs, lows = find_swings(cs, 3)

    if side == "LONG":

        supports = [
            value for _, value in lows
            if value < price
        ]

        resistances = [
            value for _, value in highs
            if value > price
        ]

        stop_base = max(supports) if supports else price - atr_value * 1.5
        sl = stop_base - atr_value * 0.15

        risk = price - sl

        if risk <= 0:
            return None

        targets = sorted(resistances)

        tp1 = targets[0] if targets else price + risk * 1.8
        tp2 = targets[1] if len(targets) > 1 else price + risk * 2.5

        if tp1 <= price:
            tp1 = price + risk * 1.5

        if tp2 <= tp1:
            tp2 = price + risk * 2.5

    else:

        resistances = [
            value for _, value in highs
            if value > price
        ]

        supports = [
            value for _, value in lows
            if value < price
        ]

        stop_base = min(resistances) if resistances else price + atr_value * 1.5
        sl = stop_base + atr_value * 0.15

        risk = sl - price

        if risk <= 0:
            return None

        targets = sorted(supports, reverse=True)

        tp1 = targets[0] if targets else price - risk * 1.8
        tp2 = targets[1] if len(targets) > 1 else price - risk * 2.5

        if tp1 >= price:
            tp1 = price - risk * 1.5

        if tp2 >= tp1:
            tp2 = price - risk * 2.5

    stop_pct = abs(price - sl) / price * 100

    if stop_pct <= 0 or stop_pct > MAX_STOP_PCT:
        return None

    fee = FEE_PCT * 2 / 100

    reward = abs(tp2 - price)
    net_reward = reward - (price + tp2) * fee
    net_risk = abs(price - sl) + (price + sl) * fee

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

    direction = "UP" if side == "LONG" else "DOWN"
    opposite = "DOWN" if side == "LONG" else "UP"

    score = 0

    # Higher timeframe trend.
    for tf, weight in (("4h", 20), ("1h", 20)):

        trend = data[tf]["trend"]

        if trend == direction:
            score += weight

        elif trend == "RANGE":
            score += weight * 0.35

    opposing = sum(
        1 for tf in ("4h", "1h")
        if data[tf]["trend"] == opposite
    )

    if opposing == 2:
        result["blockers"].append(
            "روند هر دو تایم‌فریم بالاتر مخالف است"
        )

    # EMA alignment.
    for tf, weight in (("1h", 8), ("15m", 5)):

        d = data[tf]
        e20 = d["ema20"]
        e50 = d["ema50"]

        if side == "LONG" and d["close"] > e20 > e50:
            score += weight

        if side == "SHORT" and d["close"] < e20 < e50:
            score += weight

    # RSI.
    r = data["15m"]["rsi"]

    if side == "LONG" and 50 <= r <= 68:
        score += 8

    elif side == "SHORT" and 32 <= r <= 50:
        score += 8

    # MACD.
    d = data["15m"]

    if side == "LONG" and d["macd"] > d["macd_signal"] and d["macd_hist"] > 0:
        score += 7

    if side == "SHORT" and d["macd"] < d["macd_signal"] and d["macd_hist"] < 0:
        score += 7

    # Volume.
    volume = data["15m"]["volume_ratio"]

    if volume >= 1.5:
        score += 10
    elif volume >= 1.15:
        score += 7
    elif volume >= 0.9:
        score += 4
    else:
        score += 1

    # Structure.
    cs = data["15m"]["candles"]

    sweep = detect_sweep(cs, side)
    mss = detect_mss(cs, side)

    if sweep:
        score += 10
        result["reasons"].append("Liquidity Sweep")

    if mss:
        score += 12
        result["reasons"].append("Market Structure Shift")

    if not mss:
        result["blockers"].append(
            "تأیید MSS وجود ندارد"
        )

    # Zone.
    zone = pick_zone(data, side, price)

    if zone:
        score += 8
        result["reasons"].append(
            "ناحیه " + zone["source"] + " شناسایی شد"
        )

    # Plan.
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

    score -= opposing * 8

    result["score"] = round(clamp(score), 1)
    result["sweep"] = sweep
    result["mss"] = mss
    result["zone"] = zone

    if plan and not result["blockers"] and score >= MIN_SCORE:

        result["decision"] = side
        result["plan"] = plan

    return result

# =========================================================
# SYMBOL ANALYSIS
# =========================================================

def analyze_symbol(symbol):

    symbol = validate_symbol(symbol)

    price = fetch_price(symbol)

    data = {}

    for timeframe in ("4h", "1h", "15m", "5m"):

        candles = fetch_klines(
            symbol,
            timeframe,
            CANDLE_LIMIT
        )

        data[timeframe] = build_tf(
            candles,
            timeframe
        )

    long_result = evaluate_side(
        data, "LONG", price
    )

    short_result = evaluate_side(
        data, "SHORT", price
    )

    qualified = []

    if long_result["decision"] == "LONG":
        qualified.append(long_result)

    if short_result["decision"] == "SHORT":
        qualified.append(short_result)

    # Avoid contradictory simultaneous signals.
    if len(qualified) == 1:

        signal = qualified[0]

        decision = signal["decision"]

    else:

        signal = None
        decision = "NO TRADE"

    return {
        "symbol": symbol,
        "price": price,
        "decision": decision,
        "signal": signal,
        "long": long_result,
        "short": short_result,
        "regime": market_regime(data),
        "support": liquidity_map(data["1h"], price)["support"],
        "resistance": liquidity_map(data["1h"], price)["resistance"],
        "timestamp": current_timestamp()
    }


# =========================================================
# FORMATTING
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
        return f"{value:.5f}".rstrip("0").rstrip(".")

    return f"{value:.9f}".rstrip("0").rstrip(".")


def build_card(item):

    symbol = html.escape(item["symbol"])

    lines = [
        f"<b>{symbol}</b>",
        f"قیمت: <code>{fmt_price(item['price'])}</code>",
        f"وضعیت بازار: {item['regime']}",
        f"حمایت: {fmt_price(item['support'])}",
        f"مقاومت: {fmt_price(item['resistance'])}"
    ]

    signal = item["signal"]

    if not signal:

        lines.append("<b>NO TRADE / ورود نکن</b>")

        best = max(
            (item["long"], item["short"]),
            key=lambda x: x["score"]
        )

        lines.append(
            f"جهت تحت نظر: {best['side']}"
        )

        lines.append(
            f"امتیاز: {best['score']}"
        )

        for reason in best["blockers"][:4]:
            lines.append(
                "• " + html.escape(reason)
            )

        return "\n".join(lines)

    plan = signal["plan"]

    lines.extend([
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


def format_report(results):

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    longs = sum(
        1 for x in results
        if x["decision"] == "LONG"
    )

    shorts = sum(
        1 for x in results
        if x["decision"] == "SHORT"
    )

    no_trade = len(results) - longs - shorts

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

        lines.append(build_card(item))
        lines.append("==============================")

    lines.extend([
        "",
        "<b>RISK MANAGEMENT</b>",
        f"Base risk: {RISK_PCT}%",
        f"Maximum configured leverage: {MAX_LEVERAGE}x",
        "High leverage can cause rapid liquidation.",
        "This program does not place orders."
    ])

    return "\n".join(lines)


# =========================================================
# MAIN
# =========================================================

def parse_symbols(value):

    if not value:
        return DEFAULT_SYMBOLS.copy()

    result = []

    for symbol in value.split(","):

        symbol = validate_symbol(symbol)

        if symbol not in result:
            result.append(symbol)

    return result


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

    symbols = parse_symbols(args.symbols)

    if MARKET_TYPE == "FUTURES" and not futures_adapter_configured():

        message = (
            "NO TRADE — FUTURES DATA NOT CONFIGURED. "
            "Verified Futures candle and price API settings are required."
        )

        log.error(message)

        with open(args.output, "w", encoding="utf-8") as file:
            file.write(message)

        return 1

    results = []

    for symbol in symbols:

        try:

            log.info("Scanning %s", symbol)

            result = analyze_symbol(symbol)

            results.append(result)

            log.info(
                "%s => %s",
                symbol,
                result["decision"]
            )

        except Exception as exc:

            log.exception(
                "Failed to analyze %s: %s",
                symbol,
                exc
            )

        time.sleep(REQUEST_INTERVAL)

    if not results:

        log.error("No symbols were successfully analyzed")
        return 1

    report = format_report(results)

    try:

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(report)

    except OSError as exc:

        log.error("Cannot save report: %s", exc)
        return 1

    print(report)

    return 0


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    sys.exit(main())
