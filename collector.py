import json
import requests
from pathlib import Path
from datetime import datetime, timezone

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"
WATCHLIST = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

def main():
    r = requests.get(URL, params={"productGroup": "SwapU"}, timeout=20)
    r.raise_for_status()

    data = r.json().get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Invalid API response")

    Path("data").mkdir(exist_ok=True)
    saved = 0

    with open("data/prices.jsonl", "a", encoding="utf-8") as f:
        for item in data:
            symbol = str(item.get("symbol", "")).upper()

            if symbol not in WATCHLIST:
                continue

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "lastPrice": item.get("lastPrice"),
                "markedPrice": item.get("markedPrice"),
                "openPrice": item.get("openPrice"),
                "highestPrice": item.get("highestPrice"),
                "lowestPrice": item.get("lowestPrice"),
                "volume24h": item.get("volume"),
                "turnover24h": item.get("turnover"),
                "fundingRate": item.get("prePositionFeeRate"),
                "raw": item
            }

            f.write(json.dumps(record) + "\n")
            print("LBank data:", json.dumps(item))
            saved += 1

    print("Saved records:", saved)

    if saved == 0:
        raise RuntimeError("No watched Futures symbols found")

if __name__ == "__main__":
    main()