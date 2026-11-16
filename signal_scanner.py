
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fail-closed LBank Futures feed diagnostics.

This scanner deliberately does not generate trading signals: the currently
documented public Futures feed supplies snapshots/order-book data, not verified
OHLCV candles for the strategy timeframes.
"""

import json
import math
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

MAX_AGE_SECONDS = 600
MIN_NET_RR = 1.5
EXPECTED_SOURCE = "LBank Futures"
EXPECTED_PRODUCT_GROUP = "SwapU"


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def finite_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def inspect_snapshots(now):
    counts = Counter()
    symbols = set()
    latest_timestamp = None
    latest_age = None

    if not DATA_FILE.exists():
        counts["missing_file"] += 1
        return counts, symbols, latest_timestamp, latest_age

    with DATA_FILE.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue

            counts["records_seen"] += 1
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("record is not an object")
            except (json.JSONDecodeError, ValueError):
                counts["malformed"] += 1
                continue

            if (
                item.get("source") != EXPECTED_SOURCE
                or item.get("productGroup") != EXPECTED_PRODUCT_GROUP
            ):
                counts["wrong_market_or_source"] += 1
                continue

            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol.endswith("USDT"):
                counts["invalid_symbol"] += 1
                continue

            price = finite_positive(item.get("lastPrice"))
            if price is None:
                counts["invalid_price"] += 1
                continue

            try:
                timestamp = parse_timestamp(item.get("timestamp"))
            except (TypeError, ValueError, OverflowError):
                counts["invalid_timestamp"] += 1
                continue

            age = (now - timestamp).total_seconds()
            if age < -5:
                counts["future_timestamp"] += 1
                continue
            if age > MAX_AGE_SECONDS:
                counts["stale"] += 1
                continue

            counts["fresh_valid"] += 1
            symbols.add(symbol)

            if latest_timestamp is None or timestamp > latest_timestamp:
                latest_timestamp = timestamp
                latest_age = age

    return counts, symbols, latest_timestamp, latest_age

def main():
    now = datetime.now(timezone.utc)
    counts, symbols, latest_timestamp, latest_age = inspect_snapshots(now)

    invalid_total = (
        counts["invalid_symbol"]
        + counts["invalid_price"]
        + counts["invalid_timestamp"]
    )

    lines = [
        "🎯 LBank FUTURES SNIPER",
        "MODE: SNIPER BALANCED",
        "",
        "🔴 STATUS: ENTRY DISABLED",
        "علت: منبع OHLCV معتبر Futures برای تایم‌فریم‌های استراتژی تأیید نشده است.",
        "",
        "📡 FEED DIAGNOSTICS",
        f"Records inspected: {counts['records_seen']}",
        f"Fresh valid Futures snapshots: {counts['fresh_valid']}",
        f"Stale snapshots (> {MAX_AGE_SECONDS}s): {counts['stale']}",
        f"Wrong source/market: {counts['wrong_market_or_source']}",
        f"Malformed records: {counts['malformed']}",
        f"Invalid symbol/price/timestamp: {invalid_total}",
        f"Future timestamps: {counts['future_timestamp']}",
        f"Unique fresh symbols: {len(symbols)}",
        f"Latest snapshot UTC: {latest_timestamp.isoformat() if latest_timestamp else 'N/A'}",
        f"Latest snapshot age (seconds): {round(latest_age, 1) if latest_age is not None else 'N/A'}",
        "",
        "📊 STRATEGY REQUIREMENTS (NOT YET COMPUTED)",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry: 1m",
        f"Minimum net RR: {MIN_NET_RR}",
        "EMA / RSI / MACD / volume / market structure: NOT ANALYZED",
        "LONG / SHORT setups: NOT GENERATED",

        "",
        "⚠️ DATA LIMITATION",
        "Available public Futures snapshots are not OHLCV candles.",
        "24h high/low/open values are not substitutes for timeframe candles.",
        "Spot candles and unverified TradingView extraction are forbidden.",
        "",
        "🔒 SAFETY",
        "Real orders: DISABLED",
        "No verified setup: ورود نکن",
        "Leverage warning: اهرم بالا می‌تواند باعث لیکویید شدن سریع سرمایه شود.",
        "",
        "Fresh symbols: " + (", ".join(sorted(symbols)[:15]) if symbols else "None"),
        "UTC report time: " + now.strftime("%Y-%m-%d %H:%M:%S"),
    ]

    report = "\n".join(lines)
    REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
