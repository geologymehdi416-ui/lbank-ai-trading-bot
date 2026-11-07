import json
import requests
from datetime import datetime, timezone
from pathlib import Path

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"
WATCHLIST = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

def main():
    response = requests.get(
        URL,
        params={"productGroup": "SwapU"},
        timeout=20
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

            if symbol not in WATCHLIST:
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
            print(record)
            saved += 1

    if saved == 0:
        raise Runtime