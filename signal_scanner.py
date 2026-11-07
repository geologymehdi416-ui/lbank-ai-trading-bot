import json
import requests
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

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
    price = number(item.get("lastPrice"))
    opening = number(item.get("openPrice"))
    high = number(item.get("highestPrice"))
    low = number(item.get("lowestPrice"))
    turnover = number(item.get("turnover24h"))

    if None in (price, opening, high, low, turnover):
        return None

    if opening <= 0 or high <= low or turnover < MIN_TURNOVER:
        return None

    change = (price - opening) / opening * 100
    position = (price - low) / (high - low) * 100

    if change > 1 and position > 65:
        direction = "LONG"
    elif change < -1 and position < 35:
        direction = "SHORT"
    else:
        return None

    return {
        "symbol": item["symbol"],
        "direction": direction,
        "price": price,
        "change": change,
        "position": position,
        "turnover": turnover,
        "score": abs(change) + abs(position - 50) / 20
    }


def get_orderbook(symbol):
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

        result = response.json()
        data = result.get("data", result)

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        bid_volume = sum(
            number(level.get("volume")) or 0
            for level in bids
        )

        ask_volume = sum(
            number(level.get("volume")) or 0
            for level in asks
        )

        total = bid_volume + ask_volume

        if total <= 0:
            return None

        return {
            "bid_pressure": bid_volume / total * 100,
            "ask_pressure": ask_volume / total * 100
        }

    except Exception as error:
        print(f"Orderbook error {symbol}: {error}")
        return None


def main():
    market = load_latest()
    opportunities = []

    for item in market.values():
        result = analyze(item)
        if result:
            opportunities.append(result)

    opportunities.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    report = [
        "🤖 LBank Futures Scanner",
        "🌐 بررسی قراردادهای USDT",
        f"بازارهای بررسی‌شده: {len(market)}",
        f"فرصت‌های اولیه: {len(opportunities)}",
        "━━━━━━━━━━━━━━━━"
    ]

    selected = opportunities[:MAX_RESULTS]

    if not selected:
        report.append("فرصت مناسبی پیدا نشد.")
        report.append("وضعیت: ورود نکن")

    for index, item in enumerate(selected, 1):
        book = get_orderbook(item["symbol"])

        report.append(
            f"#{index} {item['symbol']} | {item['direction']}"
        )
        report.append(f"قیمت: {item['price']:g}")
        report.append(f"تغییر ۲۴ ساعته: {item['change']:.2f}%")
        report.append(f"گردش مالی: {item['turnover']:,.0f}")

        if book:
            report.append(
                f"فشار خریدار: {book['bid_pressure']:.1f}%"
            )
            report.append(
                f"فشار فروشنده: {book['ask_pressure']:.1f}%"
            )

            if item["direction"] == "LONG":
                confirmation = (
                    "تأیید اولیه خریداران"
                    if book["bid_pressure"] > 55
                    else "تأیید نشد"
                )
            else:
                confirmation = (
                    "تأیید اولیه فروشندگان"
                    if book["ask_pressure"] > 55
                    else "تأیید نشد"
                )

            report.append(f"وضعیت دفتر سفارش: {confirmation}")
        else:
            report.append("دفتر سفارش: داده دریافت نشد")

        report.append("━━━━━━━━━━━━━━━━")

    report.extend([
        "⚠️ این بررسی سیگنال قطعی ورود نیست.",
        "⚠️ RSI، MACD و EMA هنوز محاسبه نمی‌شوند.",
        "⚠️ قبل از معامله حد ضرر تعیین کن.",
        "⚠️ از اهرم بالا استفاده نکن."
    ])

    text = "\n".join(report)
    Path("report.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()