import json
import os
import requests
from pathlib import Path

BYBIT_URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAMES = ["15", "60", "240"]


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd_histogram(closes):
    if len(closes) < 35:
        return None

    macd_values = []

    for i in range(25, len(closes) + 1):
        subset = closes[:i]
        fast = ema(subset, 12)
        slow = ema(subset, 26)
        macd_values.append(fast - slow)

    if len(macd_values) < 9:
        return None

    signal = ema(macd_values, 9)
    return macd_values[-1] - signal


def get_candles(symbol, interval):
    response = requests.get(
        BYBIT_URL,
        params={
            "