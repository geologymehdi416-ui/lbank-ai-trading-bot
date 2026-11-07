import json
import os
import requests
from statistics import mean

BYBIT_URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TIMEFRAMES = ["15", "60", "240"]

def get_candles(symbol, interval):
    response = requests.get(
        BYBIT_URL,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": 200
        },
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))

    rows = data["result"]["list"]
    rows.reverse()

    candles = []
    for row in rows[:-1]:
        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        })

    return candles

def ema(values, period):
    if len(values) < period:
        return None

    result = mean(values[:period])
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)

    return result

def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd_histogram(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None

    return fast - slow

def analyze(symbol):
    results = {}

    for tf in TIMEFRAMES:
        candles = get_candles(symbol, tf)
        closes = [c["close"] for c in candles]
        volumes = [c["volume"] for c in candles]

        results[tf] = {
            "price": closes[-1],
            "ema20": ema(closes, 20),
            "ema50": ema(closes, 50),
            "rsi": rsi(closes),
            "macd": macd_histogram(closes),
            "volume_ratio": volumes[-1] / mean(volumes[-20:]),
            "support": min(c["low"] for c in candles[-30:]),
            "resistance": max(c["high"] for c in candles[-30:])
        }

    return results

def load_lbank_prices():
    try:
        with open("data/prices.jsonl", encoding="utf-8") as file:
            records = [json.loads(line) for line in file if line.strip()]

        prices = {}
        for record in records:
            symbol = record.get("symbol")
            price = record.get("price")

            if symbol and price:
                prices[symbol] = float(price)

        return prices
    except Exception:
        return {}

def create_report():
    lbank = load_lbank_prices()
    lines = ["📊 LBank Futures Market Scanner", ""]

    for symbol in SYMBOLS:
        try:
            data = analyze(symbol)
            h4 = data["240"]
            h1 = data["60"]
            m15 = data["15"]

            lines.append(f"━━━━━━━━