import json
import requests
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"
WATCHLIST = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

response = requests.get(
    BASE_URL,
    params={"productGroup": "SwapU"},
    timeout=20
)
response.raise_for_status()

data = response.json().get("data", [])

if not isinstance(data, list):
    raise RuntimeError("Invalid API response")

now = datetime.now(timezone.utc).isoformat()
Path("data").mkdir(exist_ok=True)

saved = 0

with open("data/prices.jsonl", "a", encoding="utf-8") as output:
    for item in data:
        symbol = str(item.get("symbol", "")).upper()

        if symbol in WATCHLIST:
            record = {
                "timestamp": now,
                "symbol": symbol,
                "lastPrice": item.get("lastPrice"),
                "volume24h": item.get("volume"),
                "turnover24h": item.get("turnover")
            }

            output.write(json.dumps(record) + "\n")
            print(record)
            saved += 1

if saved == 0:
    raise RuntimeError("No Futures symbols found")

print("Saved records:", saved)