import json
import requests
import traceback
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")
ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

MIN_TURNOVER = 100000
MAX_CANDIDATES = 20
MAX_RESULTS = 5
MIN_PRESSURE = 55
MAX_DAILY_CHANGE = 15


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def analyze(item):
    price = number(item.get("lastPrice"))
    opening = number(item.get("openPrice"))
    high = number(item.get("highestPrice"))
    low = number(item.get("lowestPrice"))
    turnover = number(item.get("turnover24h"))

    if any(x is None for x in (price, opening, high, low, turnover)):
        return None

    if opening <= 0 or high <= low or turnover < MIN_TURNOVER:
        return None

    change = (price - opening) / opening * 100
    position = (price - low) / (high - low) * 100

    if abs(change) > MAX_DAILY_CHANGE:
        return None

    if change > 1 and position > 60:
        direction = "LONG"
    elif change < -1 and position < 40:
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
            params={"symbol": symbol, "depth": 20},
            timeout=10
        )
        response.raise_for_status()

        result = response.json()
        data = result.get("data", result)

        if isinstance(data, list):
            data = data[0] if data else {}

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        bid_volume = sum(
            number(level.get("volume")) or 0
            for level in bids
            if isinstance(level, dict)
        )

        ask_volume = sum(
            number(level.get("volume")) or 0
            for level in asks
            if isinstance(level, dict)
        )

        total = bid_volume + ask_volume

        if total <= 0:
            return None

        return {
            "bid": bid_volume / total * 100,
            "ask": ask_volume / total * 100
        }

    except Exception as error:
        print(f"Orderbook error {symbol}: {error}")
        return None


def main():
    report = []

    try:
        market = load_latest()
        candidates = []

        for item in market.values():
            result = analyze(item)
            if result:
                candidates.append(result)

        candidates.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        selected = []

        for item in candidates[:MAX_CANDIDATES]:
            book = get_orderbook(item["symbol"])

            if book is None:
                continue

            pressure = (
                book["bid"]
                if item["direction"] == "LONG"
                else book["ask"]
            )

            if pressure >= MIN_PRESSURE:
                item["pressure"] = pressure
                selected.append(item)

        selected.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        report = [
            "🤖 LBank Futures Scanner",
            "🌐 فیلتر قدرت حرکت و دفتر سفارش",
            f"بازارهای بررسی‌شده: {len(market)}",
            f"فرصت‌های اولیه: {len(candidates)}",
            f"فرصت‌های تأییدشده: {len(selected)}",
            "━━━━━━━━━━━━━━━━"
        ]

        for index, item in enumerate(selected[:MAX_RESULTS], 1):
            report.extend([
                f"#{index} {item['symbol']} | {item['direction']}",
                f"قیمت: {item['price']:g}",
                f"تغییر ۲۴ ساعته: {item['change']:.2f}%",
                f"موقعیت روزانه: {item['position']:.1f}%",
                f"فشار هم‌جهت دفتر سفارش: {item['pressure']:.1f}%",
                f"گردش مالی: {item['turnover']:,.0f}",
                "━━━━━━━━━━━━━━━━"
            ])

        if not selected:
            report.extend([
                "هیچ فرصت تأییدشده‌ای پیدا نشد.",
                "وضعیت: ورود نکن"
            ])

    except Exception:
        report = [
            "❌ خطا در اجرای اسکنر",
            traceback.format_exc()[-2500:]
        ]

    finally:
        report.extend([
            "⚠️ این فیلتر تأیید قطعی روند نیست.",
            "⚠️ RSI، MACD و EMA هنوز محاسبه نمی‌شوند.",
            "⚠️ قبل از ورود حد ضرر تعیین کن.",
            "⚠️ از اهرم بالا استفاده نکن."
        ])

        text = "\n".join(report)
        REPORT_FILE.write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()