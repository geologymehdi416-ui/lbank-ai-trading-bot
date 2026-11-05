import requests
from datetime import datetime

API_URL = (
    "https://lbkperp.lbank.com/"
    "cfd/openApi/v1/pub/marketData"
)

WATCHLIST = ["BTC", "ETH", "SOL"]

def scan_market():

    print("=" * 45)
    print("LBANK USDT FUTURES SCANNER")
    print(datetime.utcnow())
    print("=" * 45)

    try:
        response = requests.get(
            API_URL,
            params={"productGroup": "SwapU"},
            timeout=20
        )

        response.raise_for_status()
        result = response.json()

        if str(result.get("result")).lower() == "false":
            print("API ERROR:", result)
            return

        data = result.get("data", [])

        if not isinstance(data, list):
            print("Unexpected API response:", result)
            return

        found = 0

        for item in data:
            symbol = str(item.get("symbol", "")).upper()

            if any(coin in symbol for coin in WATCHLIST):

                print(
                    f"{symbol} | "
                    f"Price: {item.get('lastPrice')} | "
                    f"Volume: {item.get('volume')} | "
                    f"Turnover: {item.get('turnover')}"
                )

                found += 1

        if found == 0:
            print("No matching contracts found.")
            print("Available symbols:")

            for item in data[:10]:
                print(item.get("symbol"))

    except Exception as error:
        print("FUTURES API ERROR:", error)


if __name__ == "__main__":
    scan_market()