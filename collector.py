
"""
LBank USDT-M Futures market collector.

Uses ONLY the documented LBank Contract public REST API:
  GET /cfd/openApi/v1/pub/instrument
  GET /cfd/openApi/v1/pub/marketData
  GET /cfd/openApi/v1/pub/marketOrder

No Spot endpoints and no fabricated candles.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = os.getenv(
    "LBANK_FUTURES_BASE_URL",
    "https://lbkperp.lbank.com"
).rstrip("/")

PRODUCT_GROUP = os.getenv("LBANK_PRODUCT_GROUP", "SwapU")
QUOTE_CURRENCY = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "prices.jsonl"
ORDERBOOK_FILE = DATA_DIR / "orderbooks.jsonl"
CONTRACTS_FILE = DATA_DIR / "contracts.json"

ORDERBOOK_DEPTH = max(
    1, min(int(os.getenv("LBANK_ORDERBOOK_DEPTH", "5")), 50)
)
ORDERBOOK_MARKETS = max(
    0, int(os.getenv("LBANK_ORDERBOOK_MARKETS", "15"))
)

REQUEST_TIMEOUT = (10, 25)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def make_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "LBank-Futures-Collector/3.0"
    })

    return session


def get_json(
    session: requests.Session,
    path: str,
    params: Optional[dict] = None
) -> Any:

    url = f"{BASE_URL}{path}"

    response = session.get(
        url,
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Non-JSON response from {path}: {response.text[:300]}"
        ) from exc

    if isinstance(payload, dict):

        if (
            payload.get("success") is False
            or payload.get("result") is False
        ):
            raise RuntimeError(
                f"LBank API rejected {path}: "
                f"error_code={payload.get('error_code')} "
                f"msg={payload.get('msg')}"
            )

        if "data" in payload:
            return payload["data"]

    return payload


def as_list(payload: Any, label: str) -> List[dict]:

    if isinstance(payload, list):
        rows = payload

    elif isinstance(payload, dict):

        rows = None

        for key in (
            "list",
            "rows",
            "items",
            "markets",
            "instruments",
        ):

            if isinstance(payload.get(key), list):
                rows = payload[key]
                break

        if rows is None:
            raise RuntimeError(
                f"Unexpected {label} response shape: "
                f"{str(payload)[:500]}"
            )

    else:
        raise RuntimeError(
            f"Unexpected {label} response type: "
            f"{type(payload).__name__}"
        )

    return [
        row for row in rows
        if isinstance(row, dict)
    ]


def is_usdt_contract(contract: dict) -> bool:

    settlement = str(
        contract.get("clearCurrency", "")
    ).upper()

    symbol = str(
        contract.get("symbol", "")
    ).upper()

    return (
        settlement == QUOTE_CURRENCY
        or (
            not settlement
            and symbol.endswith(QUOTE_CURRENCY)
        )
    )


def append_jsonl(
    path: Path,
    records: Iterable[dict]
) -> int:

    count = 0

    with path.open("a", encoding="utf-8") as handle:

        for record in records:

            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"
            )

            count += 1

    return count


def orderbook_metrics(book: dict) -> dict:

    asks = (
        book.get("asks")
        if isinstance(book.get("asks"), list)
        else []
    )

    bids = (
        book.get("bids")
        if isinstance(book.get("bids"), list)
        else []
    )

    ask_rows = [
        (
            number(x.get("price")),
            number(x.get("volume")),
        )
        for x in asks
        if isinstance(x, dict)
    ]

    bid_rows = [
        (
            number(x.get("price")),
            number(x.get("volume")),
        )
        for x in bids
        if isinstance(x, dict)
    ]

    ask_rows = [
        (p, v)
        for p, v in ask_rows
        if p and p > 0 and v is not None and v >= 0
    ]

    bid_rows = [
        (p, v)
        for p, v in bid_rows
        if p and p > 0 and v is not None and v >= 0
    ]

    best_ask = min(
        (p for p, _ in ask_rows),
        default=None,
    )

    best_bid = max(
        (p for p, _ in bid_rows),
        default=None,
    )

    spread = None
    spread_pct = None
    mid = None

    if (
        best_ask is not None
        and best_bid is not None
        and best_ask >= best_bid
    ):

        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2

        if mid > 0:
            spread_pct = (spread / mid) * 100

    bid_depth = sum(
        p * v for p, v in bid_rows
    )

    ask_depth = sum(
        p * v for p, v in ask_rows
    )

    return {
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "midPrice": mid,
        "spread": spread,
        "spreadPct": spread_pct,
        "bidDepthQuote": bid_depth,
        "askDepthQuote": ask_depth,
        "bidLevels": len(bid_rows),
        "askLevels": len(ask_rows),
    }


def main() -> None:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    session = make_session()
    timestamp = utc_now()

    instrument_path = (
        "/cfd/openApi/v1/pub/instrument"
    )

    market_path = (
        "/cfd/openApi/v1/pub/marketData"
    )

    orderbook_path = (
        "/cfd/openApi/v1/pub/marketOrder"
    )

    # دریافت مشخصات قراردادهای Futures

    raw_contracts = as_list(
        get_json(
            session,
            instrument_path,
            {"productGroup": PRODUCT_GROUP},
        ),
        "instrument",
    )

    contracts = [
        row
        for row in raw_contracts
        if is_usdt_contract(row)
    ]

    if not contracts:
        raise RuntimeError(
            f"No {QUOTE_CURRENCY} settled Futures "
            f"contracts returned by LBank "
            f"productGroup={PRODUCT_GROUP}"
        )

    contract_by_symbol = {
        str(row.get("symbol", "")).upper(): row
        for row in contracts
        if row.get("symbol")
    }

    # دریافت قیمت‌های Futures

    raw_markets = as_list(
        get_json(
            session,
            market_path,
            {"productGroup": PRODUCT_GROUP},
        ),
        "marketData",
    )

    snapshots = []

    for market in raw_markets:

        symbol = str(
            market.get("symbol", "")
        ).upper()

        contract = contract_by_symbol.get(symbol)

        if not contract:
            continue

        last_price = number(
            market.get("lastPrice")
        )

        if last_price is None or last_price <= 0:
            continue

        snapshots.append({

            "timestamp": timestamp,

            "source": "lbank_futures_contract_api",

            "productGroup": PRODUCT_GROUP,

            "symbol": symbol,

            "baseCurrency": contract.get(
                "baseCurrency"
            ),

            "clearCurrency": contract.get(
                "clearCurrency"
            ),

            "lastPrice": last_price,

            "markedPrice": number(
                market.get("markedPrice")
            ),

            "openPrice24h": number(
                market.get("openPrice")
            ),

            "highPrice24h": number(
                market.get("highestPrice")
            ),

            "lowPrice24h": number(
                market.get("lowestPrice")
            ),

            "volume24h": number(
                market.get("volume")
            ),

            "turnover24h": number(
                market.get("turnover")
            ),

            "fundingRate": number(
                market.get("prePositionFeeRate")
            ),

            "priceTick": number(
                contract.get("priceTick")
            ),

            "volumeTick": number(
                contract.get("volumeTick")
            ),

            "volumeMultiple": number(
                contract.get("volumeMultiple")
            ),

            "minOrderVolume": number(
                contract.get("minOrderVolume")
            ),

            "minOrderCost": number(
                contract.get("minOrderCost")
            ),

            "defaultLeverage": number(
                contract.get("defaultLeverage")
            ),
        })

    if not snapshots:
        raise RuntimeError(
            "LBank returned Futures contracts but "
            "no matching valid USDT market snapshots"
        )

    # ذخیره مشخصات قراردادها

    contracts_payload = {

        "timestamp": timestamp,

        "source": "lbank_futures_contract_api",

        "productGroup": PRODUCT_GROUP,

        "quoteCurrency": QUOTE_CURRENCY,

        "count": len(contracts),

        "contracts": contracts,
    }

    CONTRACTS_FILE.write_text(
        json.dumps(
            contracts_payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ذخیره قیمت‌ها

    saved_snapshots = append_jsonl(
        SNAPSHOT_FILE,
        snapshots,
    )

    # انتخاب بازارهای دارای بیشترین گردش مالی

    ranked = sorted(
        snapshots,
        key=lambda row: row.get("turnover24h") or 0,
        reverse=True,
    )[:ORDERBOOK_MARKETS]

    # دریافت دفتر سفارش

    orderbooks = []
    errors = []

    for row in ranked:

        symbol = row["symbol"]

        try:

            raw_book = get_json(
                session,
                orderbook_path,
                {
                    "symbol": symbol,
                    "depth": ORDERBOOK_DEPTH,
                },
            )

            if not isinstance(raw_book, dict):
                raise RuntimeError(
                    "Unexpected order book shape: "
                    f"{type(raw_book).__name__}"
                )

            metrics = orderbook_metrics(raw_book)

            orderbooks.append({

                "timestamp": utc_now(),

                "source": "lbank_futures_contract_api",

                "productGroup": PRODUCT_GROUP,

                "symbol": symbol,

                **metrics,
            })

        except Exception as exc:

            errors.append({
                "symbol": symbol,
                "error": str(exc),
            })

        time.sleep(0.12)

    saved_books = append_jsonl(
        ORDERBOOK_FILE,
        orderbooks,
    )

    # گزارش اجرا

    print(
        f"LBank Futures productGroup: {PRODUCT_GROUP}"
    )

    print(
        f"USDT-settled contracts: {len(contracts)}"
    )

    print(
        f"Valid Futures market snapshots: {saved_snapshots}"
    )

    print(
        f"Order books saved: {saved_books}/{len(ranked)}"
    )

    print(
        f"Order book errors: {len(errors)}"
    )

    print(
        f"Contracts file: {CONTRACTS_FILE}"
    )

    print(
        f"Snapshot file: {SNAPSHOT_FILE}"
    )

    print(
        f"Order book file: {ORDERBOOK_FILE}"
    )

    print(
        "Candles collected: 0 "
        "(no verified Futures candle endpoint configured)"
    )

    print(
        "Technical-indicator signals must remain "
        "disabled until real Futures candles exist."
    )

    if errors:

        print("Order book error details:")

        for item in errors[:10]:

            print(
                f" - {item['symbol']}: "
                f"{item['error']}"
            )


if __name__ == "__main__":
    main()
