
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LBank Futures Snapshot Collector
Futures only - No Spot data - No order execution
"""

import json
import math
import os
import tempfile
import requests

from pathlib import Path
from datetime import datetime, timezone

BASE_URL = "https://lbkperp.lbank.com"
PRODUCT_GROUP = "SwapU"

INSTRUMENT_URL = (
    BASE_URL + "/cfd/openApi/v1/pub/instrument"
)

MARKET_URL = (
    BASE_URL + "/cfd/openApi/v1/pub/marketData"
)

DATA_FILE = Path("data/prices.jsonl")
TIMEOUT = 30


def to_number(value):
    try:
        result = float(value)

        if math.isfinite(result):
            return result

        return None

    except (TypeError, ValueError):
        return None


def get_api_data(session, url):
    response = session.get(
        url,
        params={"productGroup": PRODUCT_GROUP},
        timeout=TIMEOUT,
        headers={
            "User-Agent": "LBank-Futures-Monitor/3.0"
        },
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("Invalid API response")

    data = payload.get("data")

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected API response structure. "
            f"Response keys: {list(payload.keys())}"
        )

    return data


def get_currency(item, possible_names):
    for name in possible_names:
        value = item.get(name)

        if value is not None:
            value = str(value).strip().upper()

            if value:
                return value

    return None


def verify_contracts(instruments):
    verified = {}

    for item in instruments:

        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol", "")
        ).strip().upper()

        if not symbol:
            continue

        if not symbol.endswith("USDT"):
            continue

        quote_currency = get_currency(
            item,
            [
                "quoteCurrency",
                "priceCurrency",
                "quoteCoin",
            ],
        )

        settle_currency = get_currency(
            item,
            [
                "clearCurrency",
                "settleCurrency",
                "settlementCurrency",
            ],
        )

        # Fail closed:
        # Symbol suffix alone is not enough.
        if (
            quote_currency != "USDT"
            and settle_currency != "USDT"
        ):
            continue

        verified[symbol] = {
            "quoteCurrency": quote_currency,
            "settleCurrency": settle_currency,
            "baseCurrency": item.get("baseCurrency"),
        }

    return verified


def main():

    session = requests.Session()

    print("Connecting to LBank Futures API...")

    instruments = get_api_data(
        session,
        INSTRUMENT_URL,
    )

    market_data = get_api_data(
        session,
        MARKET_URL,
    )

    print(
        "Instrument records:",
        len(instruments),
    )

    print(
        "Market records:",
        len(market_data),
    )

    verified_contracts = verify_contracts(
        instruments
    )

    if not verified_contracts:

        sample_keys = []

        if instruments and isinstance(instruments[0], dict):
            sample_keys = list(instruments[0].keys())

        raise RuntimeError(
            "No verified USDT Futures contracts found. "
            "Instrument currency fields may differ from "
            f"the expected schema. Sample keys: {sample_keys}"
        )

    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    records = []
    skipped = 0

    for item in market_data:

        if not isinstance(item, dict):
            skipped += 1
            continue

        symbol = str(
            item.get("symbol", "")
        ).strip().upper()

        contract = verified_contracts.get(symbol)

        if contract is None:
            skipped += 1
            continue

        last_price = to_number(
            item.get("lastPrice")
        )

        if last_price is None or last_price <= 0:
            skipped += 1
            continue

        record = {
            "timestamp": timestamp,
            "source": "LBank Futures",
            "productGroup": PRODUCT_GROUP,
            "symbol": symbol,
            "contract": contract,

            "lastPrice": last_price,

            "markedPrice": to_number(
                item.get("markedPrice")
            ),

            "openPrice": to_number(
                item.get("openPrice")
            ),

            "highestPrice": to_number(
                item.get("highestPrice")
            ),

            "lowestPrice": to_number(
                item.get("lowestPrice")
            ),

            "volume24h": to_number(
                item.get("volume")
            ),

            "turnover24h": to_number(
                item.get("turnover")
            ),

            "fundingRate": to_number(
                item.get("prePositionFeeRate")
            ),

            "candleDataAvailable": False,
        }

        records.append(record)

    if not records:

        raise RuntimeError(
            "No valid Futures market snapshots found. "
            "Existing data file was not modified."
        )

    records.sort(
        key=lambda x: x.get("turnover24h") or 0,
        reverse=True,
    )

    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Atomic replacement:
    # prevents partially written data files.

    fd, temp_path = tempfile.mkstemp(
        prefix="prices_",
        suffix=".tmp",
        dir=str(DATA_FILE.parent),
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as output:

            for record in records:

                output.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
                )

        os.replace(
            temp_path,
            DATA_FILE,
        )

    finally:

        if os.path.exists(temp_path):
            os.remove(temp_path)

    print("--------------------------------")
    print("LBank Futures collection complete")
    print("--------------------------------")

    print(
        "Verified contracts:",
        len(verified_contracts),
    )

    print(
        "Saved snapshots:",
        len(records),
    )

    print(
        "Skipped records:",
        skipped,
    )

    print(
        "Data file:",
        DATA_FILE,
    )

    print("OHLCV candles: NOT collected")
    print("Trading signals: NOT generated")
    print("Real orders: DISABLED")


if __name__ == "__main__":
    main()
