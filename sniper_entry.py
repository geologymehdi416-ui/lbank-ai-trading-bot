
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

MAX_AGE_SECONDS = 600
MIN_NET_RR = 1.5


def main():
    now = datetime.now(timezone.utc)

    valid = 0
    stale = 0
    invalid = 0

    if DATA_FILE.exists():
        with DATA_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)

                    symbol = str(item.get("symbol", "")).upper()
                    price = float(item.get("lastPrice", 0))

                    if not symbol.endswith("USDT") or price <= 0:
                        invalid += 1
                        continue

                    timestamp = datetime.fromisoformat(
                        item["timestamp"].replace("Z", "+00:00")
                    )

                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)

                    age = (now - timestamp).total_seconds()

                    if age < 0 or age > MAX_AGE_SECONDS:
                        stale += 1
                    else:
                        valid += 1

                except Exception:
                    invalid += 1

    lines = [
        "🎯 LBank FUTURES SNIPER",
        "MODE: SNIPER BALANCED",
        "",
        "🔴 STATUS: ENTRY DISABLED",
        "",
        "علت: کندل معتبر Futures در تایم‌فریم 1m تأیید نشده است.",
        "",
        "📊 MARKET DATA SUMMARY",
        f"Valid price snapshots: {valid}",
        f"Stale snapshots: {stale}",
        f"Invalid records: {invalid}",
        "",
        "📈 STRATEGY REQUIREMENTS",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry: 1m",
        f"Minimum net RR: {MIN_NET_RR}",
        "",
        "⚠️ No verified Futures OHLCV candles.",
        "No EMA / RSI / MACD signal analysis performed.",
        "No LONG or SHORT recommendation.",
        "",
        "🔒 SAFETY",
        "Spot candles: FORBIDDEN",
        "Real orders: DISABLED",
        "No valid setup: ورود نکن",
        "",
        "UTC: " + now.strftime("%Y-%m-%d %H:%M:%S"),
    ]

    report = "\n".join(lines)

    REPORT_FILE.write_text(report + "\n", encoding="utf-8")

    print(report)


if __name__ == "__main__":
    main()
