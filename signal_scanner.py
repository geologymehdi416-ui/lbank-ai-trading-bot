import json
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def load_history():
    history = {symbol: [] for symbol in SYMBOLS}

    if not DATA_FILE.exists():
        return history

    with DATA_FILE.open(encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol in history:
                    history[symbol].append(item)

            except (ValueError, TypeError):
                continue

    return history


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def analyze(symbol, records):
    if not records:
        return f"{symbol}: داده‌ای موجود نیست"

    latest = records[-1]

    price = number(latest.get("lastPrice"))
    open_price = number(latest.get("openPrice"))
    high = number(latest.get("highestPrice"))
    low = number(latest.get("lowestPrice"))
    volume = number(latest.get("volume24h"))
    funding = number(latest.get("fundingRate"))

    if not price or not open_price or not high or not low:
        return f"{symbol}: داده ناقص است"

    change = (price - open_price) / open_price * 100

    if high > low:
        range_position = (price - low) / (high - low) * 100
    else:
        range_position = 50

    old_price = None

    if len(records) >= 12:
        old_price = number(records[-12].get("lastPrice"))

    momentum = None

    if old_price and old_price > 0:
        momentum = (price - old_price) / old_price * 100

    if change > 1 and range_position > 60:
        trend = "صعودی"
    elif change < -1 and range_position < 40:
        trend = "نزولی"
    else:
        trend = "خنثی"

    if trend == "صعودی":
        signal = "بررسی لانگ؛ ورود قطعی نیست"
    elif trend == "نزولی":
        signal = "بررسی شورت؛ ورود قطعی نیست"
    else:
        signal = "ورود نکن"

    lines = [
        f"📊 {symbol}",
        f"قیمت: {price:g}",
        f"تغییر ۲۴ ساعته: {change:.2f}%",
        f"روند: {trend}",
        f"وضعیت: {signal}",
        f"بالاترین قیمت ۲۴ ساعت: {high:g}",
        f"پایین‌ترین قیمت ۲۴ ساعت: {low:g}",
        f"موقعیت در محدوده ۲۴ ساعته: {range_position:.1f}%",
        f"حجم ۲۴ ساعته: {volume}",
        f"نرخ فاندینگ: {funding}",
    ]

    if momentum is not None:
        lines.append(f"تغییر نسبت به حدود یک ساعت قبل: {momentum:.2f}%")
    else:
        lines.append("تاریخچه کافی برای بررسی روند یک‌ساعته نیست")

    lines.append("━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def main():
    history = load_history()

    report = [
        "🤖 اسکنر فیوچرز LBank",
        "منبع داده: LBank Futures",
        "━━━━━━━━━━━━━━━━"
    ]

    for symbol in SYMBOLS:
        report.append(analyze(symbol, history[symbol]))

    report.append("⚠️ این گزارش سیگنال قطعی ورود نیست.")
    report.append("⚠️ از اهرم بالا استفاده نکن.")

    text = "\n".join(report)

    Path("report.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()