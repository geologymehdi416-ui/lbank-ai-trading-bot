import json
import requests
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")
ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

MAX_LATEST_AGE_MINUTES = 12
MAX_REFERENCE_LAG_MINUTES = 12
MIN_TURNOVER = 100000
DISPLAY_COUNT = 10


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_history():
    history = {}

    if not DATA_FILE.exists():
        return history

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = str(item.get("symbol", "")).upper()

                if symbol.endswith("USDT") and parse_time(item.get("timestamp")):
                    history.setdefault(symbol, []).append(item)

            except Exception:
                continue

    for records in history.values():
        records.sort(key=lambda x: parse_time(x["timestamp"]))

    return history


def timeframe_change(records, minutes, latest_time):
    target = latest_time.timestamp() - minutes * 60
    reference = None
    reference_time = None

    for item in records:
        ts = parse_time(item.get("timestamp"))

        if ts and ts.timestamp() <= target:
            reference = item
            reference_time = ts

    if reference is None:
        return None

    lag = (latest_time - reference_time).total_seconds() / 60 - minutes

    if lag > MAX_REFERENCE_LAG_MINUTES:
        return None

    old_price = number(reference.get("lastPrice"))
    current_price = number(records[-1].get("lastPrice"))

    if old_price <= 0 or current_price <= 0:
        return None

    return (current_price / old_price - 1) * 100


def orderbook_imbalance(symbol):
    try:
        response = requests.get(
            ORDERBOOK_URL,
            params={"symbol": symbol, "depth": 20},
            timeout=12
        )

        response.raise_for_status()
        data = response.json().get("data") or {}

        bids = data.get("bids") or []
        asks = data.get("asks") or []

        bid_volume = sum(number(x.get("volume")) for x in bids)
        ask_volume = sum(number(x.get("volume")) for x in asks)

        total = bid_volume + ask_volume

        if total <= 0:
            return None

        return (bid_volume - ask_volume) / total * 100

    except Exception:
        return None


def main():
    report = [
        "🤖 LBank USDT Futures Market Monitor",
        "⚠️ این گزارش سیگنال ورود نیست.",
        "━━━━━━━━━━━━━━━━"
    ]

    try:
        history = load_history()

        if not history:
            raise RuntimeError("تاریخچه قیمت موجود نیست.")

        now = datetime.now(timezone.utc)

        monitored = []
        stale_count = 0
        insufficient_count = 0

        for symbol, records in history.items():

            if not records:
                continue

            latest = records[-1]
            latest_time = parse_time(latest.get("timestamp"))

            if not latest_time:
                continue

            age = (now - latest_time).total_seconds() / 60

            if age < -2 or age > MAX_LATEST_AGE_MINUTES:
                stale_count += 1
                continue

            price = number(latest.get("lastPrice"))
            turnover = number(latest.get("turnover24h"))
            high = number(latest.get("highestPrice"))
            low = number(latest.get("lowestPrice"))
            open_price = number(latest.get("openPrice"))

            if price <= 0 or turnover < MIN_TURNOVER:
                continue

            if high <= low or open_price <= 0:
                continue

            changes = {
                "15m": timeframe_change(records, 15, latest_time),
                "1h": timeframe_change(records, 60, latest_time),
                "4h": timeframe_change(records, 240, latest_time)
            }

            if any(value is None for value in changes.values()):
                insufficient_count += 1
                continue

            change24 = (price / open_price - 1) * 100
            range_position = (price - low) / (high - low) * 100

            values = list(changes.values())

            if all(x > 0 for x in values):
                bias = "مومنتوم مثبت 🟢"
            elif all(x < 0 for x in values):
                bias = "مومنتوم منفی 🔴"
            else:
                bias = "مومنتوم ترکیبی 🟡"

            monitored.append({
                "symbol": symbol,
                "price": price,
                "turnover": turnover,
                "age": age,
                "change24": change24,
                "range": range_position,
                "changes": changes,
                "bias": bias
            })

        monitored.sort(
            key=lambda x: (
                abs(x["changes"]["4h"]) +
                abs(x["changes"]["1h"]) +
                abs(x["changes"]["15m"])
            ),
            reverse=True
        )

        report.extend([
            f"بازارهای دارای سابقه: {len(history)}",
            f"نمادهای دارای داده کامل: {len(monitored)}",
            f"نمادهای دارای داده قدیمی: {stale_count}",
            f"نمادهای با سابقه ناکافی: {insufficient_count}",
            "━━━━━━━━━━━━━━━━"
        ])

        if not monitored:

            report.append("نمادی با داده کافی پیدا نشد.")
            report.append("وضعیت: ورود نکن")

        else:

            report.append("📊 فهرست پایش بازار:")

            for item in monitored[:DISPLAY_COUNT]:

                report.extend([
                    "",
                    f"{item['symbol']} | {item['bias']}",
                    f"قیمت: {item['price']:.8g} USDT",
                    f"۱۵ دقیقه: {item['changes']['15m']:+.3f}%",
                    f"۱ ساعت: {item['changes']['1h']:+.3f}%",
                    f"۴ ساعت: {item['changes']['4h']:+.3f}%",
                    f"تغییر ۲۴ ساعت: {item['change24']:+.2f}%",
                    f"موقعیت بازه روزانه: {item['range']:.1f}%",
                    f"گردش ۲۴ ساعته: {item['turnover']:.0f}",
                    f"تازگی داده: {item['age']:.1f} دقیقه"
                ])

            report.extend([
                "",
                "📚 عدم تعادل دفتر سفارش:"
            ])

            for item in monitored[:5]:

                imbalance = orderbook_imbalance(item["symbol"])

                text = (
                    "نامشخص"
                    if imbalance is None
                    else f"{imbalance:+.1f}%"
                )

                report.append(f"{item['symbol']}: {text}")

            report.append("")
            report.append("وضعیت معاملاتی: ورود نکن")

        report.extend([
            "",
            "━━━━━━━━━━━━━━━━",
            "⚠️ اطلاعات زمانی از Snapshot پنج‌دقیقه‌ای ساخته شده‌اند.",
            "⚠️ این داده‌ها کندل رسمی فیوچرز نیستند.",
            "⚠️ RSI، MACD و EMA تا زمان دریافت کندل واقعی محاسبه نمی‌شوند.",
            "⚠️ عدم تعادل سفارش‌ها به‌تنهایی سیگنال ورود نیست.",
            "⚠️ قبل از معامله، ساختار بازار و حد ضرر را بررسی کن.",
            "⚠️ اهرم بالا ریسک لیکویید شدن را افزایش می‌دهد."
        ])

    except Exception as error:

        report.extend([
            "❌ خطا در اسکن بازار",
            str(error),
            "وضعیت معاملاتی: ورود نکن"
        ])

    REPORT_FILE.write_text(
        "\n".join(report),
        encoding="utf-8"
    )


if __name__ == "__main__":
    main()