import json
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
MIN_TURNOVER = 100000
MAX_RESULTS = 10


def load_latest():
    latest = {}

    if not DATA_FILE.exists():
        return latest

    with DATA_FILE.open(encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol and symbol.endswith("USDT"):
                    latest[symbol] = item

            except (ValueError, TypeError):
                continue

    return latest


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def analyze(item):
    symbol = item["symbol"]

    price = number(item.get("lastPrice"))
    opening = number(item.get("openPrice"))
    high = number(item.get("highestPrice"))
    low = number(item.get("lowestPrice"))
    turnover = number(item.get("turnover24h"))

    if price is None or opening is None:
        return None

    if high is None or low is None or turnover is None:
        return None

    if opening <= 0 or high <= low or turnover < MIN_TURNOVER:
        return None

    change = (price - opening) / opening * 100
    position = (price - low) / (high - low) * 100

    if change > 1 and position > 65:
        direction = "LONG"
        score = abs(change) + position / 20

    elif change < -1 and position < 35:
        direction = "SHORT"
        score = abs(change) + (100 - position) / 20

    else:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "change": change,
        "position": position,
        "turnover": turnover,
        "score": score
    }


def main():
    market = load_latest()
    opportunities = []

    for item in market.values():
        result = analyze(item)

        if result is not None:
            opportunities.append(result)

    opportunities.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = opportunities[:MAX_RESULTS]

    report = [
        "🤖 LBank Futures Scanner",
        "🌐 بررسی تمام قراردادهای USDT",
        f"تعداد بازارهای بررسی‌شده: {len(market)}",
        f"تعداد فرصت‌های پیدا‌شده: {len(opportunities)}",
        "━━━━━━━━━━━━━━━━"
    ]

    if not selected:
        report.append("فرصت معاملاتی مناسبی پیدا نشد.")
        report.append("وضعیت: ورود نکن")

    for index, item in enumerate(selected, 1):
        report.extend([
            f"#{index} {item['symbol']} | {item['direction']}",
            f"قیمت: {item['price']:g}",
            f"تغییر ۲۴ ساعته: {item['change']:.2f}%",
            f"موقعیت در محدوده روزانه: {item['position']:.1f}%",
            f"گردش مالی: {item['turnover']:,.0f}",
            f"امتیاز اولیه: {item['score']:.2f}",
            "━━━━━━━━━━━━━━━━"
        ])

    report.extend([
        "⚠️ امتیازها فقط برای رتبه‌بندی اولیه هستند.",
        "⚠️ قبل از ورود، ساختار بازار و حد ضرر را بررسی کن.",
        "⚠️ از اهرم بالا استفاده نکن."
    ])

    text = "\n".join(report)

    Path("report.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()