
import os
import json
import time
from datetime import datetime, timezone

import requests


FUTURES_BASE = os.getenv(
    "LBANK_FUTURES_BASE",
    "https://lbkperp.lbank.com"
).rstrip("/")

PRODUCT_GROUP = os.getenv("LBANK_PRODUCT_GROUP", "SwapU")

DATA_DIR = "data"
INSTRUMENTS_FILE = os.path.join(DATA_DIR, "instruments.json")
MARKET_DATA_FILE = os.path.join(DATA_DIR, "market_data.json")
PRICES_FILE = os.path.join(DATA_DIR, "prices.jsonl")

TIMEOUT = 20

session = requests.Session()
session.headers.update({
    "User-Agent": "LBank-Futures-Collector/2.0",
    "Accept": "application/json",
})


def request_json(endpoint, params=None):
    url = f"{FUTURES_BASE}{endpoint}"

    response = session.get(
        url,
        params=params or {},
        timeout=TIMEOUT
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid JSON response from {endpoint}"
        ) from exc

    if isinstance(payload, dict):
        error_code = payload.get("error_code")

        if error_code not in (None, 0, "0", ""):
            raise RuntimeError(
                f"LBank API error: {payload}"
            )

        if payload.get("success") is False:
            raise RuntimeError(
                f"LBank request failed: {payload}"
            )

        if payload.get("result") is False:
            raise RuntimeError(
                f"LBank request failed: {payload}"
            )

    return payload


def extract_list(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response type")

    for key in ("data", "result", "records"):
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for nested_key in ("data", "list", "records"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested

    raise RuntimeError(
        f"Could not find a data list: {str(payload)[:1000]}"
    )


def get_instruments():
    payload = request_json(
        "/cfd/openApi/v1/pub/instrument",
        params={"productGroup": PRODUCT_GROUP}
    )

    instruments = extract_list(payload)

    valid = [
        item for item in instruments
        if isinstance(item, dict) and item.get("symbol")
    ]

    if not valid:
        raise RuntimeError("No valid Futures instruments received")

    return valid


def get_market_data():
    payload = request_json(
        "/cfd/openApi/v1/pub/marketData",
        params={"productGroup": PRODUCT_GROUP}
    )

    records = extract_list(payload)

    valid = [
        item for item in records
        if isinstance(item, dict) and item.get("symbol")
    ]

    if not valid:
        raise RuntimeError("No valid Futures market data received")

    return valid


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)

    temporary_path = path + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temporary_path, path)


def save_prices(records):
    os.makedirs(DATA_DIR, exist_ok=True)

    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    with open(PRICES_FILE, "w", encoding="utf-8") as file:
        for item in records:
            symbol = item.get("symbol")
            last_price = item.get("lastPrice")

            if not symbol or last_price in (None, ""):
                continue

            try:
                price = float(last_price)
            except (ValueError, TypeError):
                continue

            if price <= 0:
                continue

            try:
                quote_volume = float(item.get("turnover") or 0)
            except (ValueError, TypeError):
                quote_volume = 0.0

            try:
                volume = float(item.get("volume") or 0)
            except (ValueError, TypeError):
                volume = 0.0

            row = {
                "symbol": symbol,
                "price": price,
                "quote_volume": quote_volume,
                "volume": volume,
                "high_24h": item.get("highestPrice"),
                "low_24h": item.get("lowestPrice"),
                "mark_price": item.get("markedPrice"),
                "open_24h": item.get("openPrice"),
                "funding_rate": item.get("prePositionFeeRate"),
                "timestamp": timestamp,
                "source": "LBank Futures"
            }

            file.write(
                json.dumps(row, ensure_ascii=False) + "\n"
            )


def main():
    print("=" * 55)
    print("LBANK FUTURES DATA COLLECTOR")
    print("=" * 55)

    os.makedirs(DATA_DIR, exist_ok=True)

    print("Fetching Futures instruments...")
    instruments = get_instruments()

    print(f"Instruments received: {len(instruments)}")

    print("Fetching Futures market data...")
    market_data = get_market_data()

    print(f"Market records received: {len(market_data)}")

    instrument_symbols = {
        str(item["symbol"]).upper()
        for item in instruments
        if item.get("symbol")
    }

    matched_market_data = [
        item for item in market_data
        if str(item.get("symbol", "")).upper()
        in instrument_symbols
    ]

    if not matched_market_data:
        raise RuntimeError(
            "No matching Futures instruments and market records"
        )

    save_json(INSTRUMENTS_FILE, instruments)
    save_json(MARKET_DATA_FILE, matched_market_data)
    save_prices(matched_market_data)

    print(f"Matched contracts: {len(matched_market_data)}")
    print(f"Saved: {INSTRUMENTS_FILE}")
    print(f"Saved: {MARKET_DATA_FILE}")
    print(f"Saved: {PRICES_FILE}")

    print("=" * 55)
    print("COLLECTION COMPLETED")
    print("WARNING: These are market quotes, not candles.")
    print("Candle-based scanner must remain disabled.")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"COLLECTOR FAILED: {exc}")
        raise
