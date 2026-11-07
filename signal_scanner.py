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
                    ["timestamp
# ============================================================
# ORDER BLOCK
# ============================================================

def find_order_block(candles, fvg, direction):
    """
    Heuristic Order Block:
    Last opposing candle before the FVG formation.
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
                "type": "BE
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
    if (
        h4["bullish"] if is_long else h4["bearish"]
    ):
        score += 15
        reasons.append("4H trend aligned")

    if (
        h1["bullish"] if is_long else h1["bearish"]
    ):
        score += 15
        reasons.append("1H trend aligned")

    # Liquidity sweep
    score += 15
    reasons.append("Liquidity sweep confirmed")

    # Market structure shift
    score += 15
    reasons.append("MSS confirmed")

    # FVG
    if setup["zone"].get("fvg"):
        score += 10
        reasons.append("FVG identified")

    # Order block
    if setup["zone"].get("ob"):
        score += 5
        reasons.append("Order Block overlap")

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

        if (is_long and hist > 0) or (
            not is_long and hist < 0
        ):
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
       # ============================================================
# REPORT FORMATTING
# ============================================================

def format_trade(trade):
    return "\n".join([
        f"Direction: {trade['direction']}",
        f"Entry: {fmt(trade['entry'])}",
        f"Entry Zone: {fmt(trade['zone_low'])} - {fmt(trade['zone_high'])}",
        f"Stop Loss: {fmt(trade['stop'])}",
        f"TP1: {fmt(trade['tp1'])} | 1R",
        f"TP2: {fmt(trade['tp2'])} | 2R",
        f"TP3: {fmt(trade['tp3'])}",
        f"Liquidity Target: {fmt(trade['liquidity_target'])}",
        f"Stop Distance: {trade['risk_percent']:.2f}%",
        f"Leverage: {trade['leverage']}x ISOLATED",
        f"Maximum Account Risk: {RISK_PER_TRADE_PERCENT}%"
    ])


def format_market_status(item):
    symbol = item.get("symbol", "UNKNOWN")
    status = item.get("status", "UNKNOWN")

    lines = [
        f"• {symbol} | {status}",
        f"Price: {fmt(item.get('price'))}",
        f"Score: {item.get('score', 0)}/100",
    ]

    if item.get("reason"):
        lines.append(f"Reason: {item['reason