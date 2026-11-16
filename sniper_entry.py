
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LBank Futures Sniper - Balanced Mode

Futures-only safety gate.
No Spot candles.
No order execution.
No entry signals without verified Futures 1m OHLCV.

Current mode:
ENTRY DISABLED until a documented and validated
LBank Futures 1m candle endpoint is available.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

MAX_SNAPSHOT_AGE_SECONDS = 600

MIN_NET_RR = 1.5

TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")


def parse_timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        return None


def load_latest_futures_prices():
    latest = {}

    if not DATA_FILE.exists():
        return latest

    with DATA_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)

                symbol = str(item.get("symbol", "")).upper()
                timestamp = parse_timestamp(item.get("timestamp", ""))
                price = float(item.get("lastPrice", 0))

                if not symbol.endswith("USDT"):
                    continue

                if timestamp is None or price <= 0:
                    continue

                previous = latest.get(symbol)

                if previous is None or timestamp > previous["timestamp"]:
                    latest[symbol] = {
                        "timestamp": timestamp,
                        "price": price,
                    }

            except (ValueError, TypeError, json.JSONDecodeError):
                continue

    return latest


def build_report():
    now = datetime.now(timezone.utc)

    lines = [
        "🎯 LBank FUTURES SNIPER",
        "MODE: BALANCED",
        "STATUS: ENTRY DISABLED",
        "",
        "⚠️ ورود غیرفعال است.",
        "",
        "دلیل:",
        "منبع معتبر کندل 1m فیوچرز هنوز تأیید نشده است.",
        "داده Spot برای تحلیل یا صدور سیگنال استفاده نمی‌شود.",
        "",
        "تنظیمات استراتژی:",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry trigger: 1m",
        "Minimum net RR: %.2f" % MIN_NET_RR,
        "Order execution: DISABLED",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📊 FUTURES MARKET SNAPSHOTS",
        "",
    ]

    markets = load_latest_futures_prices()

    if not markets:
        lines.append("داده معتبر Futures در دسترس نیست.")
    else:
        for symbol in sorted(markets):
            item = markets[symbol]

            age = (now - item["timestamp"]).total_seconds()

            if age < 0 or age > MAX_SNAPSHOT_AGE_SECONDS:
                status = "داده قدیمی؛ غیرقابل استفاده"
            else:
                status = "قیمت معتبر؛ بدون تأیید کندل 1m"

            lines.append(
                "%s | Price: %s | %s"
                % (symbol, item["price"], status)
            )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🔒 SAFETY RULES",
        "",
        "1. فقط داده واقعی Futures مجاز است.",
        "2. کندل Spot برای ورود ممنوع است.",
        "3. نبود کندل معتبر 1m = عدم صدور ورود.",
        "4. RR خالص کمتر از 1.5 = رد معامله.",
        "5. سفارش واقعی ثبت نمی‌شود.",
        "",
        "زمان بررسی UTC: %s" % now.strftime("%Y-%m-%d %H:%M:%S"),
    ])

    return "\n".join(lines)


def main():
    report = build_report()

    REPORT_FILE.write_text(report + "\n", encoding="utf-8")

    print(report)

    print("\nSniper safety gate completed.")
    print("No orders were placed.")


if __name__ == "__main__":
    main()
