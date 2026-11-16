
import json
import os
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.lbank.com"
FUTURES_BASE = "https://lbkperp.lbank.com"

DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "prices.jsonl")

TIMEOUT = 15
RETRIES = 3
SLEEP_BETWEEN_REQUESTS = 0.15

session = requests.Session()
session.headers.update({
    "User-Agent": "LBank-Futures-Scanner/1.0",
    "Accept": "application/json",
})


def request_json(url, params=None):
    for attempt in range(RETRIES):
        try:
            response = session.get(
                url,
                params=params,
                timeout=TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, (dict, list)):
                raise ValueError("Invalid API response")

            return data

        except Exception as exc:
            print(
                f"Request failed ({attempt + 1}/{RETRIES}): {exc}"
            )
            time.sleep(1 + attempt)

    return None


def extract_list(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("data", "result", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                for nested in ("data", "list", "records"):
                    if isinstance(value.get(nested), list):
                        return value[nested]

    return []


def get_instruments():
    url = f"{FUTURES_BASE}/cfd/openApi/v1/pub/instrument"
    data = request_json(url)

    if data is None:
        raise RuntimeError("Could not retrieve Futures instruments")

    instruments = extract_list(data)

    if not instruments:
        raise RuntimeError(
            "Instrument response format is not verified"
        )

    return instruments


def get_market_data():
    url = f"{FUTURES_BASE}/cfd/openApi/v1/pub/marketData"
    data = request_json(url)

    if data is None:
        raise RuntimeError("Could not retrieve Futures market data")

    records = extract_list(data)

    if not records:
        raise RuntimeError(
            "Market data response format is not verified"
        )

    return records


def get_symbol(record):
    for key in ("symbol", "instrumentId", "contractCode", "name"):
        value = record.get(key)
        if value:
            return str(value).upper()
    return None


def get_number(record, keys):
    for key in keys:
        value = record.get(key)
        if value is None:
            continue

        try:
            number = float(value)
            if number > 0:
                return number
        except (TypeError, ValueError):
            continue

    return None


def normalize_market_record(record):
    symbol = get_symbol(record)

    if not symbol:
        return None

    price = get_number(
        record,
        ("lastPrice", "last", "close", "price", "last_price")
    )

    if price is None:
        return None

    volume = get_number(
        record,
        ("volume", "vol", "baseVolume", "turnover", "amount")
    )

    quote_volume = get_number(
        record,
        ("quoteVolume", "quoteVol", "turnover24h", "amount24h")
    )

    return {
        "symbol": symbol,
        "price": price,
        "volume": volume,
        "quote_volume": quote_volume,
        "raw": record,
    }


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print("Loading LBank Futures instruments...")
    instruments = get_instruments()

    print("Loading LBank Futures market data...")
    market_records = get_market_data()

    instrument_symbols = {
        get_symbol(item)
        for item in instruments
        if get_symbol(item)
    }

    timestamp = datetime.now(timezone.utc).isoformat()

    saved = 0
    skipped = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as file:
        for record in market_records:
            normalized = normalize_market_record(record)

            if normalized is None:
                skipped += 1
                continue

            symbol = normalized["symbol"]

            if symbol not in instrument_symbols:
                skipped += 1
                continue

            output = {
                "timestamp": timestamp,
                "source": "LBank Futures",
                **normalized,
            }

            file.write(
                json.dumps(output, ensure_ascii=False) + "\n"
            )
            saved += 1

    print("--------------------------------")
    print("LBank Futures Collector")
    print(f"Instruments: {len(instrument_symbols)}")
    print(f"Market records: {len(market_records)}")
    print(f"Saved: {saved}")
    print(f"Skipped: {skipped}")
    print(f"Output: {OUTPUT_FILE}")
    print("--------------------------------")


if __name__ == "__main__":
    main()
