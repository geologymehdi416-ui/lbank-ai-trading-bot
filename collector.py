import json
import math
import requests
from pathlib import Path
from datetime import datetime, timezone

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData"
DATA_FILE = Path("data/prices.jsonl")


def to_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def main():
    response = requests.get(
        URL,
        params={"productGroup": "SwapU"},
        timeout=30,
        headers={"User-Agent": "LBank-Futures-Monitor/2.1"},
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid LBank API response")

    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected LBank response: {payload}")

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    saved = 0
    skipped = 0

    with DATA_FILE.open("a", encoding="utf-8") as output:
        for item in data:
            if not isinstance(item, dict):
                skipped += 1
                continue

            symbol = str(item.get("symbol", "")).upper().strip()
            if not symbol.endswith("USDT"):
                continue

            last_price = to_number(item.get("lastPrice"))
            if last_price is None or last_price <= 0:
                skipped += 1
                continue

            record = {
                "timestamp": timestamp,
                "symbol": symbol,
                "lastPrice": last_price,
                "markedPrice": to_number(item.get("markedPrice")),
                "openPrice": to_number(item.get("openPrice")),
                "highestPrice": to_number(item.get("highestPrice")),
                "lowestPrice": to_number(item.get("lowestPrice")),
                "volume24h": to_number(item.get("volume")),
                "turnover24h": to_number(item.get("turnover")),
                "fundingRate": to_number(item.get("prePositionFeeRate")),
            }
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
            saved += 1

    print(f"USDT Futures markets received: {len(data)}")
    print(f"Valid market snapshots saved: {saved}")
    print(f"Invalid records skipped: {skipped}")

    if saved == 0:
        raise RuntimeError("LBank returned no valid USDT Futures market records")


if __name__ == "__main__":
    main()