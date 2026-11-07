import json
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
MIN_TURNOVER = 100000
MAX_RESULTS = 10


def load_latest():
    latest = {}

    if not DATA_FILE.exists():
        return latest

    with DATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol:
                    latest[symbol] = item
            except (ValueError, TypeError):
                continue

    return latest


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def analyze(item):
    symbol = item["symbol"]

    price = num(item.get("lastPrice"))
    opening = num(item.get("openPrice"))
    high = num(item.get("highestPrice"))
    low = num(item.get("lowestPrice"))
    turnover = num(item.get("turnover24h"))

    if not all([price, opening, high, low, turnover]):
        return None

    if turnover < MIN_TURNOVER or high <= low:
        return None

    change = (price - opening) / opening * 100
    position = (price - low) / (high - low) * 100

    if change > 1 and position > 65:
        direction = "LONG"
        score = abs(change) + position / 20
    elif change < -1 and position < 35:
        direction = "SHORT"
        score