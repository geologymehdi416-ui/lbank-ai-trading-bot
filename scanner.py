import requests
from datetime import datetime, timezone

BASE_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub"

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
PRODUCT_GROUP = "SwapU"
DEPTH = 5

SESSION = requests.Session()

def check_response(result, name):
if not isinstance(result, dict):
raise Exception(f"{name}: Invalid response format")

if result.get("result") is False or str(result.get("result")).lower() == "false":
    raise Exception(f"{name}: {result}")

error_code = result.get("error_code")

if error_code not in (None, 0, "0"):
    raise Exception(f"{name}: {result}")

return result.get("data")

def get_market_data():
url = f"{BASE_URL}/marketData"

response = SESSION.get(
    url,
    params={"productGroup": PRODUCT_GROUP},
    timeout=20
)

response.raise_for_status()
result = response.json()

data = check_response(result, "Market API")

if not isinstance(data, list):
    raise Exception(f"Unexpected market data: {result}")

return data

def get_order_book(symbol):
url = f"{BASE_URL}/marketOrder"

response = SESSION.get(
    url,
    params={
        "symbol": symbol,
        "depth": DEPTH
    },
    timeout=20
)

response.raise_for_status()
result = response.json()

data = check_response(result, "Order Book API")

if not isinstance(data, dict):
    raise Exception(f"Invalid order book response: {result}")

asks = data.get("asks", [])
bids = data.get("bids", [])

return asks, bids

def get_price_level(level):
if isinstance(level, dict):
return (
float(level["price"]),
float(level["volume"])
)

if isinstance(level, (list, tuple)) and len(level) >= 2:
    return float(level[0]), float(level[1])

raise ValueError(f"Unknown order book level: {level}")

def analyze_order_book(asks, bids):
if not asks or not bids:
print("ORDER BOOK EMPTY")
print("Asks received:", len(asks))
print("Bids received:", len(bids))
return

ask_levels = [get_price_level(x) for x in asks]
bid_levels = [get_price_level(x) for x in bids]

best_ask = min(ask_levels, key=lambda x: x[0])
best_bid = max(bid_levels, key=lambda x: x[0])

ask_volume = sum(x[1] for x in ask_levels)
bid_volume = sum(x[1] for x in bid_levels)

spread = best_ask[0] - best_bid[0]

spread_percent = (
    spread / best_bid[0] * 100
    if best_bid[0] > 0 else 0
)

total_volume = ask_volume + bid_volume

imbalance = (
    (bid_volume - ask_volume) / total_volume * 100
    if total_volume > 0 else 0
)

print("\nORDER BOOK ANALYSIS")
print("Best Bid:", best_bid[0])
print("Best Ask:", best_ask[0])
print("Spread:", round(spread, 8))
print("Spread %:", round(spread_percent, 5))
print("Bid Volume:", round(bid_volume, 6))
print("Ask Volume:", round(ask_volume, 6))
print("Book Imbalance %:", round(imbalance, 2))

if imbalance > 10:
    print("Book Pressure: BUY SIDE")
elif imbalance < -10:
    print("Book Pressure: SELL SIDE")
else:
    print("Book Pressure: BALANCED")

def scan_market():
print("=" * 55)
print("LBANK USDT FUTURES SCANNER")
print(datetime.now(timezone.utc).isoformat())
print("=" * 55)

try:
    market_data = get_market_data()

    market_map = {
        str(item.get("symbol", "")).upper(): item
        for item in market_data
    }

    for symbol in WATCHLIST:
        print("\n" + "-" * 50)
        print("SYMBOL:", symbol)

        market = market_map.get(symbol)

        if not market:
            print("Market data not found.")
            continue

        try:
            last_price = float(market.get("lastPrice", 0))
            mark_price = float(market.get("markedPrice", 0))
            volume = float(market.get("volume", 0))
            turnover = float(market.get("turnover", 0))

            print("Last Price:", last_price)
            print("Mark Price:", mark_price)
            print("24h Volume:", volume)
            print("24h Turnover:", turnover)

        except (TypeError, ValueError) as error:
            print("MARKET DATA ERROR:", error)
            continue

        try:
            asks, bids = get_order_book(symbol)
            analyze_order_book(asks, bids)

        except Exception as error:
            print("ORDER BOOK ERROR:", error)

except Exception as error:
    print("SCANNER ERROR:", error)

print("\nSCAN COMPLETED")

if name == "main":
scan_market()