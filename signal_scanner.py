import json
import requests
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

MIN_TURNOVER = 100000
MAX_CANDIDATES = 20
MAX_RESULTS = 5
MIN_PRESSURE = 55
MAX_DAILY_CHANGE = 15


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_latest():
    latest = {}

    if not DATA_FILE.exists():
        return latest

    with DATA_FILE.open(encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol and symbol.endswith("USDT"):
                    latest[symbol] = item

            except (ValueError, TypeError):
                continue

    return latest


def analyze(item):
    price = number(item.get("lastPrice"))
    opening = number(item.get("openPrice"))
    high = number(item.get("highestPrice"))
    low = number(item.get("lowestPrice"))
    turnover = number(item.get("turnover24h"))

    if None in (price, opening, high, low, turnover):
        return None

    if opening <= 0 or high <= low:
        return None

    if turnover < MIN_TURNOVER:
        return None

    change = (price - opening) / opening * 100
    position = (price - low) / (high - low) * 100

    if abs(change) > MAX_DAILY_CHANGE:
        return None

    if change > 1 and position > 60:
        direction = "LONG"
    elif change < -1 and position < 40:
        direction