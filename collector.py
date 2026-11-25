#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LBank USDT-M Futures collector.
Only documented LBank Contract public REST endpoints are used.
Collects snapshots and order books for a fixed watchlist of up to 20 assets.
No Spot endpoints and no fabricated candles.
"""
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = os.getenv("LBANK_FUTURES_BASE_URL", "https://lbkperp.lbank.com").rstrip("/")
PRODUCT_GROUP = os.getenv("LBANK_PRODUCT_GROUP", "SwapU")
QUOTE_CURRENCY = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()
WATCHLIST = [
    x.strip().upper()
    for x in os.getenv(
        "LBANK_WATCHLIST",
        "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,AVAX,LINK,LTC,HYPE,SUI,AAVE,UNI,NEAR,ICP,RENDER,ONDO,PYTH,JUP"
    ).split(",")
    if x.strip()
][:20]

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "prices.jsonl"
ORDERBOOK_FILE = DATA_DIR / "orderbooks.jsonl"
CONTRACTS_FILE = DATA_DIR / "contracts.json"

ORDERBOOK_DEPTH = max(1, min(int(os.getenv("LBANK_ORDERBOOK_DEPTH", "10")), 50))
REQUEST_TIMEOUT = (10, 25)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def norm_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def contract_base(contract: dict) -> str:
    base = str(contract.get("baseCurrency") or "").upper().strip()
    if base:
        return base
    symbol = norm_symbol(contract.get("symbol"))
    quote = norm_symbol(QUOTE_CURRENCY)
    return symbol[:-len(quote)] if quote and symbol.endswith(quote) else symbol


def is_usdt_contract(contract: dict) -> bool:
    settlement = str(contract.get("clearCurrency") or "").upper()
    symbol = norm_symbol(contract.get("symbol"))
    quote = norm_symbol(QUOTE_CURRENCY)
    return settlement == QUOTE_CURRENCY or (not settlement and bool(quote) and symbol.endswith(quote))


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=2, backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "LBank-Futures-Collector/4.0"})
    return session


def get_json(session: requests.Session, path: str, params: Optional[dict] = None) -> Any:
    response = session.get(f"{BASE_URL}{path}", params=params or {}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {path}: {response.text[:300]}") from exc

    if isinstance(payload, dict):
        if payload.get("success") is False or payload.get("result") is False:
            raise RuntimeError(
                f"LBank API rejected {path}: error_code={payload.get('error_code')} msg={payload.get('msg')}"
            )
        if "data" in payload:
            return payload["data"]
    return payload


def as_list(payload: Any, label: str) -> List[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("list", "rows", "items", "markets", "instruments"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if rows is None:
            raise RuntimeError(f"Unexpected {label} response shape: {str(payload)[:500]}")
    else:
        raise RuntimeError(f"Unexpected {label} response type: {type(payload).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def append_jsonl(path: Path, records: Iterable[dict]) -> int:
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def level_pair(level: Any):
    """Accept documented object levels and common [price, volume] array levels."""
    if isinstance(level, dict):
        return number(level.get("price")), number(level.get("volume", level.get("amount", level.get("size"))))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return number(level[0]), number(level[1])
    return None, None


def orderbook_metrics(book: dict) -> dict:
    asks = book.get("asks") if isinstance(book.get("asks"), list) else []
    bids = book.get("bids") if isinstance(book.get("bids"), list) else []
    ask_rows = [level_pair(x) for x in asks]
    bid_rows = [level_pair(x) for x in bids]
    ask_rows = [(p, v) for p, v in ask_rows if p is not None and p > 0 and v is not None and v >= 0]
    bid_rows = [(p, v) for p, v in bid_rows if p is not None and p > 0 and v is not None and v >= 0]

    best_ask = min((p for p, _ in ask_rows), default=None)
    best_bid = max((p for p, _ in bid_rows), default=None)
    spread = spread_pct = mid = None
    if best_ask is not None and best_bid is not None and best_ask > best_bid:
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2
        spread_pct = spread / mid * 100 if mid > 0 else None

    return {
        "bestBid": best_bid, "bestAsk": best_ask, "midPrice": mid,
        "spread": spread, "spreadPct": spread_pct,
        "bidDepthQuote": sum(p * v for p, v in bid_rows),
        "askDepthQuote": sum(p * v for p, v in ask_rows),
        "bidLevels": len(bid_rows), "askLevels": len(ask_rows),
        "rawLevelFormat": "object_or_price_volume_array",
    }


def main() -> None:
    if not WATCHLIST:
        raise RuntimeError("LBANK_WATCHLIST is empty")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()
    timestamp = utc_now()

    instrument_path = "/cfd/openApi/v1/pub/instrument"
    market_path = "/cfd/openApi/v1/pub/marketData"
    orderbook_path = "/cfd/openApi/v1/pub/marketOrder"

    raw_contracts = as_list(
        get_json(session, instrument_path, {"productGroup": PRODUCT_GROUP}), "instrument"
    )
    usdt_contracts = [x for x in raw_contracts if is_usdt_contract(x)]
    contract_by_symbol = {
        norm_symbol(x.get("symbol")): x for x in usdt_contracts if x.get("symbol")
    }

    # Match the 20 requested base assets against LBank's actual contract metadata.
    selected_contracts = {}
    for symbol, contract in contract_by_symbol.items():
        base = contract_base(contract)
        if base in WATCHLIST:
            selected_contracts[symbol] = contract

    selected_bases_found = {contract_base(x) for x in selected_contracts.values()}
    missing_assets = [x for x in WATCHLIST if x not in selected_bases_found]
    if not selected_contracts:
        raise RuntimeError(
            "None of the requested watchlist assets matched active USDT-settled contract metadata"
        )

    raw_markets = as_list(
        get_json(session, market_path, {"productGroup": PRODUCT_GROUP}), "marketData"
    )
    market_by_symbol = {
        norm_symbol(x.get("symbol")): x for x in raw_markets if x.get("symbol")
    }

    snapshots = []
    for normalized, contract in selected_contracts.items():
        market = market_by_symbol.get(normalized)
        if not market:
            continue
        last_price = number(market.get("lastPrice"))
        if last_price is None or last_price <= 0:
            continue
        snapshots.append({
            "timestamp": timestamp,
            "source": "lbank_futures_contract_api",
            "productGroup": PRODUCT_GROUP,
            "symbol": str(contract.get("symbol")),
            "baseCurrency": contract.get("baseCurrency") or contract_base(contract),
            "clearCurrency": contract.get("clearCurrency"),
            "lastPrice": last_price,
            "markedPrice": number(market.get("markedPrice")),
            "openPrice24h": number(market.get("openPrice")),
            "highPrice24h": number(market.get("highestPrice")),
            "lowPrice24h": number(market.get("lowestPrice")),
            "volume24h": number(market.get("volume")),
            "turnover24h": number(market.get("turnover")),
            "fundingRate": number(market.get("prePositionFeeRate")),
            "priceTick": number(contract.get("priceTick")),
            "volumeTick": number(contract.get("volumeTick")),
            "volumeMultiple": number(contract.get("volumeMultiple")),
            "minOrderVolume": number(contract.get("minOrderVolume")),
            "minOrderCost": number(contract.get("minOrderCost")),
            "defaultLeverage": number(contract.get("defaultLeverage")),
        })

    if not snapshots:
        raise RuntimeError("No valid market snapshots matched the selected Futures watchlist")

    contracts_payload = {
        "timestamp": timestamp, "source": "lbank_futures_contract_api",
        "productGroup": PRODUCT_GROUP, "quoteCurrency": QUOTE_CURRENCY,
        "watchlist": WATCHLIST, "count": len(selected_contracts),
        "contracts": list(selected_contracts.values()),
        "missing_watchlist_assets": missing_assets,
    }
    CONTRACTS_FILE.write_text(json.dumps(contracts_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    saved_snapshots = append_jsonl(SNAPSHOT_FILE, snapshots)

    # Fetch a book for every selected market snapshot; no top-turnover truncation.
    orderbooks, errors = [], []
    for row in snapshots:
        symbol = row["symbol"]
        try:
            raw_book = get_json(
                session, orderbook_path,
                {"symbol": symbol, "depth": ORDERBOOK_DEPTH},
            )
            if not isinstance(raw_book, dict):
                raise RuntimeError(f"Unexpected order book response type: {type(raw_book).__name__}")
            # Some wrappers return the actual book one level deeper.
            if not isinstance(raw_book.get("asks"), list) and isinstance(raw_book.get("data"), dict):
                raw_book = raw_book["data"]
            metrics = orderbook_metrics(raw_book)
            orderbooks.append({
                "timestamp": utc_now(), "source": "lbank_futures_contract_api",
                "productGroup": PRODUCT_GROUP, "symbol": symbol, **metrics,
            })
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
        time.sleep(0.12)

    saved_books = append_jsonl(ORDERBOOK_FILE, orderbooks)

    print(f"LBank Futures productGroup: {PRODUCT_GROUP}")
    print(f"Requested watchlist: {len(WATCHLIST)}")
    print(f"USDT-settled contracts available: {len(usdt_contracts)}")
    print(f"Selected watchlist contracts: {len(selected_contracts)}")
    print(f"Valid Futures market snapshots: {saved_snapshots}")
    print(f"Order books saved: {saved_books}/{len(snapshots)}")
    print(f"Order book errors: {len(errors)}")
    print(f"Watchlist assets missing from contract list: {','.join(missing_assets) if missing_assets else 'none'}")
    print(f"Contracts file: {CONTRACTS_FILE}")
    print(f"Snapshot file: {SNAPSHOT_FILE}")
    print(f"Order book file: {ORDERBOOK_FILE}")
    print("Candles collected: 0 (no verified Futures candle endpoint configured)")
    print("Technical-indicator signals remain disabled until real Futures candles exist.")
    if errors:
        print("Order book error details:")
        for item in errors:
            print(f" - {item['symbol']}: {item['error']}")


if __name__ == "__main__":
    main()
