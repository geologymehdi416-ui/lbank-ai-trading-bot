import requests
from datetime import datetime, timezone

BASE_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub"
WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
PRODUCT_GROUP = "SwapU"

SESSION = requests.Session()
def get_market_data():
    url = f"{BASE_URL}/marketData"
    response = SESSION.get(
        url,
        params={"productGroup": PRODUCT_GROUP},
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    data = result.get("data", [])

    if not isinstance(data, list):
        raise Exception(f"Invalid market data: {result}")

    return data
def scan_market():
    print("=" * 45)
    print("LBANK USDT FUTURES SCANNER")
    print(datetime.now(timezone.utc).isoformat())

    try:
        data = get_market_data()

        for item in data:
            symbol = str(item.get("symbol", "")).upper()

            if symbol in WATCHLIST:
                print("-" * 40)
                print("Symbol:", symbol)
                print("Price:", item.get("lastPrice"))
                print("Volume:", item.get("volume"))
                print("Turnover:", item.get("turnover"))

    except Exception as error:
        print("SCANNER ERROR:", error)

    print("SCAN COMPLETED")


if __name__ == "__main__":
    scan_market()