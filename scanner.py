import requests
import time
from datetime import datetime

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

API_URL = "https://api.lbkex.com/v2/ticker/24hr.do"

def scan_market():
    print("=" * 40)
    print("LBANK FUTURES SCANNER")
    print(datetime.now())
    print("=" * 40)

    for symbol in SYMBOLS:
        try:
            response = requests.get(
                API_URL,
                params={"symbol": symbol.lower()},
                timeout=15
            )

            data = response.json()

            if data.get("result") != "true":
                print(symbol, "API response:", data)
                continue

            ticker = data["data"]
            price = float(ticker["ticker"]["latest"])
            change = float(ticker["ticker"]["change"])

            print(f"{symbol} | Price: {price} | Change: {change}%")

        except Exception as error:
            print(f"{symbol} ERROR: {error}")

if __name__ == "__main__":
    scan_market()