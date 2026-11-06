import json
from pathlib import Path

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

def ema(prices, period):
alpha = 2 / (period + 1)
result = prices[0]
for price in prices[1:]:
result = alpha * price + (1 - alpha) * result
return result

def main():
history = Path("data/prices.jsonl")

if not history.exists():
    print("No price history")
    return

records = {symbol: [] for symbol in WATCHLIST}

with history.open(encoding="utf-8") as file:
    for line in file:
        try:
            item = json.loads(line)
            symbol = item["symbol"]
            price = float(item["lastPrice"])
            if symbol in records and price > 0:
                records[symbol].append(price)
        except (ValueError, KeyError, TypeError):
            continue

for symbol, prices in records.items():
    print("\n--------------------")
    print("SYMBOL:", symbol)

    if len(prices) < 50:
        print("DECISION: WAIT")
        continue

    fast = ema(prices[-50:], 12)
    slow = ema(prices[-50:], 26)

    print("PRICE:", prices[-1])
    print("EMA12:", round(fast, 4))
    print("EMA26:", round(slow, 4))

    if fast > slow and prices[-1] > fast:
        print("BIAS: LONG")
    elif fast < slow and prices[-1] < fast:
        print("BIAS: SHORT")
    else:
        print("DECISION: NO TRADE")

print("\nSCAN COMPLETED")

if name == "main":
main()
