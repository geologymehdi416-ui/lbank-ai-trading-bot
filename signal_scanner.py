import json
from pathlib import Path

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HISTORY = Path("data/prices.jsonl")

def ema(values, period):
    result = []
    alpha = 2 / (period + 1)

    for value in values:
        if not result:
            result.append(value)
        else:
            result.append(alpha * value + (1 - alpha) * result[-1])

    return result

def calculate_rsi(prices):
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    gain = sum(gains[-14:]) / 14
    loss = sum(losses[-14:]) / 14

    if loss == 0:
        return 100

    return 100 - (100 / (1 + gain / loss))

def main():
    if not HISTORY.exists():
        print("Price history not found")
        return

    records = {symbol: [] for symbol in WATCHLIST}

    with HISTORY.open