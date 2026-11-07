import json
import requests
from pathlib import Path
from datetime import datetime, timezone

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"

def main():
    response = requests.get(
        URL,
        params={"productGroup": "SwapU"},
        timeout=30
    )
    response.raise_for_status()

    data = response.json().get("data", [])

    if not isinstance(data, list):
        raise RuntimeError("Invalid LBank response")

    Path("data").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    saved = 0

    with open("data/prices.jsonl", "a", encoding="utf-8") as f:
        for item in data:
            symbol = str(item.get("symbol", "")).upper()

            if not symbol.endswith("USDT"):
                continue

            record = {
                "timestamp": now,
                "symbol": symbol,
                "lastPrice": item.get("lastPrice"),
                "markedPrice": item.get("markedPrice"),
                "openPrice": item.get("openPrice"),
                "highestPrice": item.get("highestPrice"),
                "lowestPrice": item.get("lowestPrice"),
                "volume24h": item.get("volume"),
                "turnover24h": item.get("turnover"),
                "fundingRate": item.get("prePositionFeeRate")
            }

            f.write(json.dumps(record) + "\n")
            saved += 1

    print("Total USDT Futures saved:", saved)

    if saved == 0:
        raise RuntimeError("No USDT Futures symbols found")

if __name__ == "__main__":
    main()