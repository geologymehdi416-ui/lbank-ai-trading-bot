import json
from pathlib import Path

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HISTORY = Path("data/prices.jsonl")

def ema(values, period):
if not values:
return []

alpha = 2 / (period + 1)
result = [values[0]]

for value in values[1:]:
    result.append(alpha * value + (1 - alpha) * result[-1])

return result

def calculate_rsi(prices, period=14):
if len(prices) <= period:
return None

changes = [
    prices[i] - prices[i - 1]
    for i in range(1, len(prices))
]

gains = [max(change, 0) for change in changes]
losses = [max(-change, 0) for change in changes]

average_gain = sum(gains[-period:]) / period
average_loss = sum(losses[-period:]) / period

if average_loss == 0:
    return 100.0 if average_gain > 0 else 50.0

relative_strength = average_gain / average_loss
return 100 - (100 / (1 + relative_strength))

def load_history():
records = {symbol: [] for symbol in WATCHLIST}

if not HISTORY.exists():
    print("ERROR: Price history file not found.")
    return records

with HISTORY.open(encoding="utf-8") as file:
    for line in file:
        try:
            item = json.loads(line)

            symbol = str(item.get("symbol", "")).upper()
            price = float(item.get("lastPrice"))

            if symbol in records and price > 0:
                records[symbol].append(price)

        except (ValueError, TypeError, json.JSONDecodeError):
            continue

return records

def analyze_symbol(symbol, prices):
print("\n" + "=" * 40)
print("SYMBOL:", symbol)

if len(prices) < 50:
    print("DECISION: WAIT")
    print("Reason: Not enough price history.")
    print("Available snapshots:", len(prices))
    return

fast = ema(prices, 12)
slow = ema(prices, 26)

ema20 = ema(prices, 20)[-1]
ema50 = ema(prices, 50)[-1]

macd_values = [
    fast[i] - slow[i]
    for i in range(len(prices))
]

macd = macd_values[-1]
signal = ema(macd_values, 9)[-1]
rsi = calculate_rsi(prices)

current_price = prices[-1]

print("Price:", current_price)
print("RSI:", round(rsi, 2))
print("EMA20:", round(ema20, 4))
print("EMA50:", round(ema50, 4))
print("MACD:", round(macd, 6))
print("Signal:", round(signal, 6))

if (
    current_price > ema20 > ema50
    and macd > signal
    and rsi is not None
    and 50 < rsi < 70
):
    print("BIAS: LONG")

elif (
    current_price < ema20 < ema50
    and macd < signal
    and rsi is not None
    and 30 < rsi < 50
):
    print("BIAS: SHORT")

else:
    print("DECISION: NO TRADE")

def main():
print("=" * 45)
print("LBANK FUTURES TECHNICAL SCANNER")
print("=" * 45)

records = load_history()

for symbol in WATCHLIST:
    analyze_symbol(symbol, records[symbol])

print("\nSCAN COMPLETED")
print("Note: Preliminary analysis, not an execution signal.")

if name == "main":
main()