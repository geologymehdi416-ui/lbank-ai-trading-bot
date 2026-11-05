import requests
from datetime import datetime, timezone

BASE_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub"

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DEPTH = 5


def get_market_data():
    url = f"{BASE_URL}/marketData"

    response = requests.get(
        url,
        params={"productGroup": "SwapU"},
        timeout=20
    )

    response.raise_for_status()
    result = response.json()

    if str(result.get("result")).lower() != "true":
        raise Exception(f"Market API error: {result}")

    return result.get("data", [])


def get_order_book(symbol):
    url = f"{BASE_URL}/marketOrder"

    response = requests.get(
        url,
        params={
            "symbol": symbol,
            "depth": DEPTH
        },
        timeout=20
    )

    response.raise_for_status()
    result = response.json()

    if result.get("error_code", 0) not in (0, "0"):
        raise Exception(f"Order book API error: {result}")

    return result


def scan_market():

    print("=" * 55)
    print("LBANK USDT FUTURES SCANNER")
    print(datetime.now(timezone.utc).isoformat())
    print("=" * 55)

    try:
        market_data = get_market_data()

        if not isinstance(market_data, list):
            raise Exception("Unexpected market data format")

        market_map = {
            str(item.get("symbol", "")).upper(): item
            for item in market_data
        }

        for symbol in WATCHLIST:

            print("\n" + "-" * 45)
            print("SYMBOL:", symbol)

            market = market_map.get(symbol)

            if not market:
                print("Market data not found.")
                continue

            price = float(market.get("lastPrice", 0))
            volume = float(market.get("volume", 0))
            turnover = float(market.get("turnover", 0))
            mark_price = float(market.get("markedPrice", 0))

            print("Last Price:", price)
            print("Mark Price:", mark_price)
            print("24h Volume:", volume)
            print("24h Turnover:", turnover)

            try:
                book = get_order_book(symbol)

                asks = book.get("asks", [])
                bids = book.get("bids", [])

                if not asks or not bids:
                    print("Order book is empty.")
                    continue

                best_ask = min(
                    asks,
                    key=lambda x: float(x["price"])
                )

                best_bid = max(
                    bids,
                    key=lambda x: float(x["price"])
                )

                ask_price = float(best_ask["price"])
                bid_price = float(best_bid["price"])

                ask_volume = sum(
                    float(x["volume"]) for x in asks
                )

                bid_volume = sum(
                    float(x["volume"]) for x in bids
                )

                spread = ask_price - bid_price

                spread_percent = (
                    spread / bid_price * 100
                    if bid_price else 0
                )

                total_volume = bid_volume + ask_volume

                imbalance = (
                    (bid_volume - ask_volume) / total_volume * 100
                    if total_volume else 0
                )

                print("\nORDER BOOK")
                print("Best Bid:", bid_price)
                print("Best Ask:", ask_price)
                print("Spread:", spread)
                print("Spread %:", round(spread_percent, 4))
                print("Bid Volume:", round(bid_volume, 4))
                print("Ask Volume:", round(ask_volume, 4))
                print("Book Imbalance %:", round(imbalance, 2))

            except Exception as error:
                print("ORDER BOOK ERROR:", error)

    except Exception as error:
        print("SCANNER ERROR:", error)


if __name__ == "__main__":
    scan_market()