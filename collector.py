
PRODUCT_GROUP = "SwapU"

def get_instruments():
    url = f"{FUTURES_BASE}/cfd/openApi/v1/pub/instrument"

    data = request_json(
        url,
        params={"productGroup": PRODUCT_GROUP}
    )

    if data is None:
        raise RuntimeError("Could not retrieve Futures instruments")

    if isinstance(data, dict):
        if data.get("error_code") not in (None, 0, "0"):
            raise RuntimeError(
                f"LBank instrument error: {data}"
            )

        if data.get("success") is False:
            raise RuntimeError(
                f"LBank instrument request failed: {data}"
            )

    instruments = extract_list(data)

    if not instruments:
        raise RuntimeError(
            f"Instrument response is empty or unexpected: "
            f"{str(data)[:1000]}"
        )

    return instruments


def get_market_data():
    url = f"{FUTURES_BASE}/cfd/openApi/v1/pub/marketData"

    data = request_json(
        url,
        params={"productGroup": PRODUCT_GROUP}
    )

    if data is None:
        raise RuntimeError("Could not retrieve Futures market data")

    if isinstance(data, dict):
        if data.get("error_code") not in (None, 0, "0"):
            raise RuntimeError(
                f"LBank market data error: {data}"
            )

        if data.get("success") is False:
            raise RuntimeError(
                f"LBank market data request failed: {data}"
            )

    records = extract_list(data)

    if not records:
        raise RuntimeError(
            f"Market data response is empty or unexpected: "
            f"{str(data)[:1000]}"
        )

    return records
