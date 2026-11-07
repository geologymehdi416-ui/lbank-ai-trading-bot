#!/usr/bin/env python3
import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==========================================
# SETTINGS
# ==========================================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OKX_URL = "https://www.okx.com/api/v5/market/candles"

MAX_SYMBOLS = 30
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75
MAX_SNAPSHOT_AGE_MINUTES = 20

MAX_RETRIES = 3
MAX_WORKERS = 5

# فیلتر حجم
MIN_VOLUME_RATIO = 1.2
MIN_15M_VOLUME_RATIO = 0.8

# مدیریت ریسک
MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0
MIN_REWARD_RISK = 1.0

# واچ‌لیست
MIN_WATCH_SCORE = 55
MAX_WATCH_PER_SIDE = 5

# نمادهای شناخته‌شده غیرکریپتویی یا ETF
BLOCKED_SYMBOLS = {
    "SOXSUSDT",
    "SOXLUSDT",
    "QQQUSDT",
    "TQQQUSDT",
    "SKHYNIXUSDT",
    "SKHYUSDT",
    "SNXXUSDT",
    "MUSTOCKUSDT"
}

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H"
}

HEADERS = {
    "User-Agent": "LBank-Futures-Signal-Bot/8.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ==========================================
# EXCEPTIONS
# ==========================================

class SkipSymbol(Exception):
    pass


# ==========================================
# UTILITIES
# ==========================================

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fmt(value):
    if value is None:
        return "ناموجود"
    return f"{float(value):,.8g}"


def clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


def safe_div(a, b):
    if b is None or b == 0:
        return 0
    return a / b


# ==========================================
# INDICATORS
# ==========================================

def ema_series(values, period):

    output = [None] * len(values)

    if len(values) < period:
        return output

    current = sum(values[:period]) / period
    output[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * alpha
            + current * (1 - alpha)
        )
        output[i] = current

    return output


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
        return 100 if avg_gain > 0 else 50

    return 100 - (
        100 / (1 + avg_gain / avg_loss)
    )


def macd(values):

    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    line = []

    for f, s in zip(fast, slow):

        if f is not None and s is not None:
            line.append(f - s)

    if len(line) < 9:
        return None, None, None

    signal_series = ema_series(line, 9)

    macd_value = line[-1]
    signal_value = signal_series[-1]

    if signal_value is None:
        return None, None, None

    histogram = macd_value - signal_value

    return macd_value, signal_value, histogram


def atr(candles, period=14):

    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):

        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        ranges.append(tr)

    return sum(ranges[-period:]) / period


# ==========================================
# LBANK DATA
# ==========================================

def load_latest_lbank():

    if not DATA_FILE.exists():
        raise RuntimeError("LBank data file not found")

    latest = {}

    with DATA_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        for line in file:

            try:

                row = json.loads(line)

                symbol = str(
    row.get("symbol", "")
).strip().upper()