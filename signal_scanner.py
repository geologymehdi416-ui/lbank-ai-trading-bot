import json
import requests
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

MAX_LATEST_AGE_MINUTES = 12
MAX_REFERENCE_LAG_MINUTES = 12
MAX_GAP_MINUTES = 12

MIN_TURNOVER = 100000
MIN_4H_RECORDS = 20
DISPLAY_COUNT = 10
DIAGNOSTIC_COUNT = 12


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value):
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def load_history():
    history = {}
    invalid_lines = 0

    if not DATA_FILE.exists():
        return history, invalid_lines

    with DATA_FILE.open("r", encoding="utf-8") as file:

        for line in file:

            try:
                item = json.loads(line)

                symbol = str(
                    item.get("symbol", "")
                ).upper()

                timestamp = parse_time(
                    item.get("timestamp")
                )

                if symbol.endswith("USDT") and timestamp:
                    history.setdefault(
                        symbol, []
                    ).append(item)

                else:
                    invalid_lines += 1

            except Exception:
                invalid_lines += 1

    for records in history.values():

        records.sort(
            key=lambda x: parse_time(
                x.get("timestamp")
            )
        )

    return history, invalid_lines


def timeframe_change(records, minutes, latest_time):

    target = latest_time.timestamp() - minutes * 60

    reference = None
    reference_time = None

    for item in records:

        ts = parse_time(
            item.get("timestamp")
        )

        if ts and ts.timestamp() <= target:
            reference = item
            reference_time = ts

    if reference is None:
        return None

    lag = (
        latest_time - reference_time
    ).total_seconds() / 60 - minutes

    if lag < 0 or lag > MAX_REFERENCE_LAG_MINUTES:
        return None

    old_price = number(
        reference.get("lastPrice")
    )

    current_price = number(
        records[-1].get("lastPrice")
    )

    if old_price <= 0 or current_price <= 0:
        return None

    return (
        current_price / old_price - 1
    ) * 100


def history_diagnostics(records, now):

    if not records:
        return {
            "reason": "بدون سابقه",
            "span": 0,
            "count4h": 0,
            "max_gap": 0
        }

    latest_time = parse_time(
        records[-1].get("timestamp")
    )

    if latest_time is None:
        return {
            "reason": "زمان نامعتبر",
            "span": 0,
            "count4h": 0,
            "max_gap": 0
        }

    span = (
        latest_time -
        parse_time(records[0].get("timestamp"))
    ).total_seconds() / 3600

    window_start = latest_time.timestamp() - 240 * 60

    recent = []

    for item in records:

        ts = parse_time(item.get("timestamp"))

        if ts and ts.timestamp() >= window_start:
            recent.append(ts)

    max_gap = 0

    for i in range(1, len(recent)):

        gap = (
            recent[i] - recent[i - 1]
        ).total_seconds() / 60

        max_gap = max(max_gap, gap)

    return {
        "span": span,
        "count4h": len(recent),
        "max_gap": max_gap
    }


def orderbook_imbalance(symbol):

    try:

        response = requests.get(
            ORDERBOOK_URL,
            params={
                "symbol": symbol,
                "depth": 20
            },
            timeout=10
        )

        response.raise_for_status()

        data = response.json().get("data") or {}

        bids = data.get("bids") or []
        asks = data.get("asks") or []

        bid_volume = sum(
            number(x.get("volume"))
            for x in bids
        )

        ask_volume = sum(
            number(x.get("volume"))
            for x in asks
        )

        total = bid_volume + ask_volume

        if total <= 0:
            return None

        return (
            (bid_volume - ask_volume) / total
        ) * 100

    except Exception:
        return None


def main():

    now = datetime.now(timezone.utc)

    report = [
        "🤖 LBank USDT Futures Smart Monitor",
        "⚠️ این گزارش سیگنال ورود نیست.",
        "━━━━━━━━━━━━━━━━"
    ]

    try:

        history, invalid_lines = load_history()

        if not history:
            raise RuntimeError(
                "هیچ تاریخچه معتبری پیدا نشد."
            )

        monitored = []
        rejected = []

        stale_count = 0
        insufficient_count = 0
        turnover_count = 0
        invalid_price_count = 0

        for symbol, records in history.items():

            if not records:
                continue

            latest = records[-1]

            latest_time = parse_time(
                latest.get("timestamp")
            )

            if latest_time is None:
                continue

            age = (
                now - latest_time
            ).total_seconds() / 60

            diagnostics = history_diagnostics(
                records, now
            )

            reasons = []

            if age < -2 or age > MAX_LATEST_AGE_MINUTES:

                stale_count += 1
                reasons.append("داده قدیمی")

            price = number(
                latest.get("lastPrice")
            )

            turnover = number(
                latest.get("turnover24h")
            )

            high = number(
                latest.get("highestPrice")
            )

            low = number(
                latest.get("lowestPrice")
            )

            open_price = number(
                latest.get("openPrice")
            )

            if price <= 0:
                invalid_price_count += 1
                reasons.append("قیمت نامعتبر")

            if turnover < MIN_TURNOVER:
                turnover_count += 1
                reasons.append("گردش مالی ناکافی")

            if diagnostics["span"] < 4:
                reasons.append("سابقه کمتر از ۴ ساعت")

            if diagnostics["count4h"] < MIN_4H_RECORDS:
                reasons.append("تعداد نمونه ناکافی")

            if diagnostics["max_gap"] > MAX_GAP_MINUTES:
                reasons.append("وقفه در داده‌ها")

            if high <= low or open_price <= 0:
                reasons.append("اطلاعات قیمت ناقص")

            changes = {
                "15m": timeframe_change(
                    records, 15, latest_time
                ),

                "1h": timeframe_change(
                    records, 60, latest_time
                ),

                "4h": timeframe_change(
                    records, 240, latest_time
                )
            }

            for timeframe, value in changes.items():

                if value is None:
                    reasons.append(
                        f"داده مرجع {timeframe} موجود نیست"
                    )

            if reasons:

                insufficient_count += 1

                rejected.append({
                    "symbol": symbol,
                    "reasons": list(dict.fromkeys(reasons)),
                    "count": len(records),
                    "span": diagnostics["span"],
                    "gap": diagnostics["max_gap"],
                    "age": age
                })

                continue

            change24 = (
                price / open_price - 1
            ) * 100

            range_position = (
                (price - low) /
                (high - low)
            ) * 100

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

        rejected.sort(
            key=lambda x: (
                x["span"],
                x["count"]
            ),
            reverse=True
        )

        report.extend([
            f"بازارهای دارای سابقه: {len(history)}",
            f"نمادهای دارای داده کامل: {len(monitored)}",
            f"نمادهای ردشده: {len(rejected)}",
            f"نمادهای دارای داده قدیمی: {stale_count}",
            f"نمادهای با سابقه ناکافی: {insufficient_count}",
            f"گردش مالی ناکافی: {turnover_count}",
            f"قیمت نامعتبر: {invalid_price_count}",
            f"خطوط نامعتبر فایل: {invalid_lines}",
            "━━━━━━━━━━━━━━━━"
        ])

        if monitored:

            report.append(
                "📊 فهرست نمادهای قابل پایش:"
            )

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
                    f"گردش مالی: {item['turnover']:.0f}",
                    f"تازگی داده: {item['age']:.1f} دقیقه"
                ])

            report.extend([
                "",
                "📚 عدم تعادل دفتر سفارش:"
            ])

            for item in monitored[:5]:

                imbalance = orderbook_imbalance(
                    item["symbol"]
                )

                value = (
                    "نامشخص"
                    if imbalance is None
                    else f"{imbalance:+.1f}%"
                )

                report.append(
                    f"{item['symbol']}: {value}"
                )

        else:

            report.append(
                "❌ هیچ نمادی شرایط پایش کامل را ندارد."
            )

        if rejected:

            report.extend([
                "",
                "🔎 نمونه نمادهای ردشده:"
            ])

            for item in rejected[:DIAGNOSTIC_COUNT]:

                report.extend([
                    "",
                    f"{item['symbol']}:",
                    f"تعداد رکورد: {item['count']}",
                    f"طول سابقه: {item['span']:.2f} ساعت",
                    f"بیشترین وقفه: {item['gap']:.1f} دقیقه",
                    f"آخرین داده: {item['age']:.1f} دقیقه قبل",
                    "علت: " + "، ".join(item["reasons"][:3])
                ])

        report.extend([
            "",
            "━━━━━━━━━━━━━━━━",
            "وضعیت معاملاتی: ورود نکن",
            "⚠️ داده‌ها Snapshot هستند، نه کندل رسمی فیوچرز.",
            "⚠️ RSI، MACD و EMA محاسبه نشده‌اند.",
            "⚠️ این گزارش به‌تنهایی مبنای ورود نیست.",
            "⚠️ اهرم بالا ریسک لیکویید شدن را افزایش می‌دهد."
        ])

    except Exception as error:

        report.extend([
            "",
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