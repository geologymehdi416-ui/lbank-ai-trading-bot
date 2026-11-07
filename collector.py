import json
import requests
from pathlib import Path
from datetime import datetime, timezone

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"

def main():
response = requests.get(
URL,
params={"productGroup": "SwapU"},
timeout=30,
headers={"User-Agent": "LBank-Futures-Monitor/2.0"}
)
response.raise_for_status()

result = response.json()

if not isinstance(result, dict):
    raise RuntimeError("Invalid API response format")

if result.get("result") is False:
    raise RuntimeError(f"LBank API error: {result}")

data = result.get("data", [])

if not isinstance(data, list):
    raise RuntimeError("Invalid LBank market data")

Path("data").mkdir(exist_ok=True)

now = datetime.now(timezone.utc).isoformat()
saved = 0
skipped = 0

with open("data/prices.jsonl", "a", encoding="utf-8") as f:
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue

        symbol = str(item.get("symbol", "")).upper()

        if not symbol.endswith("USDT"):
            continue

        price = item.get("lastPrice")

        if price is None:
            skipped += 1
            continue

        record = {
            "timestamp": now,
            "symbol": symbol,
            "lastPrice": price,
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

print(f"USDT Futures markets received: {len(data)}")
print(f"Markets saved: {saved}")
print(f"Records skipped: {skipped}")

if saved == 0:
    raise RuntimeError("No valid USDT Futures markets found")

if name == "main":
main()