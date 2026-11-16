#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LBank Futures market snapshot collector.

Uses ONLY the documented public Futures contract endpoints:
  /cfd/openApi/v1/pub/instrument
  /cfd/openApi/v1/pub/marketData

This collector does NOT create candles and does NOT place orders.
It fails closed when contract metadata cannot confirm a USDT-quoted contract.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://lbkperp.lbank.com"
PRODUCT_GROUP = "SwapU"
INSTRUMENT_URL = f"{BASE_URL}/cfd/openApi/v1/pub/instrument"
MARKET_URL = f"{BASE_URL}/cfd/openApi/v1/pub/marketData"
DATA_FILE = Path("data/prices.jsonl")
TIMEOUT = 25


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def extract_list(payload, label):
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}: response is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"{label}: expected data[]; got keys={list(payload.keys())}")
    return data


def get_json(session, url):
    response = session.get(
        url,
        params={"productGroup": PRODUCT_GROUP},
        timeout=TIMEOUT,
        headers={"User-Agent": "LBank-Futures-Snapshot-Monitor/3.0"},
    )
    response.raise_for_status()
    return response.json()


def main():
    session = requests.Session()

    instruments = extract_list(
        get_json(session, INSTRUMENT_URL), "Futures instrument"
    )
    market_rows = extract_list(
        get_json(session, MARKET_URL), "Futures marketData"
    )

    # A symbol is accepted only if the official Futures instrument endpoint
    # lists it and its contract metadata explicitly identifies USDT as a
    # pricing or settlement currency. A USDT suffix alone is not sufficient.
    valid_contracts = {}
    for item in instruments:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol.endswith("USDT"):
            continue

        price_currency = str(item.get("priceCurrency", "")).strip().upper()
        clear_currency = str(item.get("clearCurrency", "")).strip().upper()
        if "USDT" not in (price_currency, clear_currency):
            continue

        valid_contracts[symbol] = {
            "baseCurrency": item.get("baseCurrency"),
            "priceCurrency": price_currency,
            "clearCurrency": clear_currency,
            "priceTick": number(item.get("priceTick")),
            "volumeTick": number(item.get("volumeTick")),
            "minOrderVolume": number(item.get("minOrderVolume")),
            "volumeMultiple": number(item.get("volumeMultiple")),
        }

    timestamp = datetime.now(timezone.utc).isoformat()
    records = []
    skipped = 0

    for item in market_rows:
        if not isinstance(item, dict):
            skipped += 1
            continue

        symbol = str(item.get("symbol", "")).strip().upper()
        contract = valid_contracts.get(symbol)
        if contract is None:
            skipped += 1
            continue

        last_price = number(item.get("lastPrice"))
        turnover = number(item.get("turnover"))
        volume = number(item.get("volume"))
        if last_price is None or last_price <= 0:
            skipped += 1
            continue

        records.append({
            "timestamp": timestamp,
            "source": "LBank Futures public marketData",
            "productGroup": PRODUCT_GROUP,
            "symbol": symbol,
            "contract": contract,
            "lastPrice": last_price,
            "markedPrice": number(item.get("markedPrice")),
            "openPrice24h": number(item.get("openPrice")),
            "highPrice24h": number(item.get("highestPrice")),
            "lowPrice24h": number(item.get("lowestPrice")),
            "volume24h": volume,
            "turnover24h": turnover,
            "fundingRate": number(item.get("prePositionFeeRate")),
            "candleDataAvailable": False,
        })

    if not records:
        raise RuntimeError(
            "No verified USDT Futures contracts found. "
            "No data file was written; check API response and contract metadata."
        )

    records.sort(key=lambda row: (row["turnover24h"] or 0), reverse=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Replace the previous snapshot instead of endlessly appending duplicates.
    fd, temp_name = tempfile.mkstemp(
        prefix="prices.", suffix=".tmp", dir=str(DATA_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(temp_name, DATA_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    print(f"Instrument metadata rows: {len(instruments)}")
    print(f"Market rows received: {len(market_rows)}")
    print(f"Verified USDT Futures snapshots saved: {len(records)}")
    print(f"Rows skipped: {skipped}")
    print("Candle data: NOT available from this collector")
    print("Order execution: DISABLED")


if __name__ == "__main__":
    main()
