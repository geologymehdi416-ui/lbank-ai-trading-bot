import json
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

MIN_TURNOVER = 100000
MAX_CANDIDATES = 20
MAX_RESULTS = 5
MIN_PRESSURE = 55
MAX_DAILY_CHANGE = 15

TIMEFRAMES = {
    "15m": (15, 10, 0.05),
    "1h": (60, 15, 0.10),
    "4h": (240, 30, 0.20)
}


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except (ValueError, TypeError, AttributeError):
        return None


def load_market():
    latest = {}
    history = {name: {} for name in TIMEFRAMES}

    if not DATA_FILE.exists():
        return latest, history

    now = datetime.now(timezone.utc)

    cutoffs = {
        name: now - timedelta(minutes=minutes)
        for name, (minutes, _, _) in TIMEFRAMES.items()
    }

    with DATA_FILE.open(encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")
                timestamp = parse_time(item.get("timestamp"))
                price = number(item.get("lastPrice"))

                if not symbol or not symbol.endswith("USDT"):
                    continue

                if timestamp is None or price is None or price <= 0:
                    continue

                current = latest.get(symbol)

                if current is None or timestamp > current["_time"]:
                    item["_time"] = timestamp
                    latest[symbol] = item

                for name, cutoff in cutoffs.items():
                    if timestamp > cutoff:
                        continue

                    previous = history[name].get(symbol)

                    if previous is None or timestamp > previous["_time"]:
                        history[name][symbol] = {
                            "price": price,
                            "_time": timestamp
                        }

            except (ValueError, TypeError):
                continue

    return latest, history


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
        "turnover": turnover
    }


def check_trend(symbol, price, history):
    now = datetime.now(timezone.utc)
    results = {}

    for name, (minutes, max_age, minimum) in TIMEFRAMES.items():
        old = history[name].get(symbol)

        if old is None:
            return None

        age = now - old["_time"]

        if age > timedelta(minutes=minutes + max_age):
            return None

        old_price = old["price"]

        if old_price <= 0:
            return None

        change = (price - old_price) / old_price * 100
        results[name] = change

    return results


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
        market, history = load_market()
        candidates = []

        for item in market.values():
            result = analyze(item)

            if result:
                candidates.append(result)

        candidates.sort(
            key=lambda x: abs(x["change"]),
            reverse=True
        )

        selected = []
        missing_history = 0

        for item in candidates[:MAX_CANDIDATES]:
            trends = check_trend(
                item["symbol"],
                item["price"],
                history
            )

            if trends is None:
                missing_history += 1
                continue

            if item["direction"] == "LONG":
                aligned = all(
                    trends[name] >= minimum
                    for name, (_, _, minimum) in TIMEFRAMES.items()
                )
            else:
                aligned = all(
                    trends[name] <= -minimum
                    for name, (_, _, minimum) in TIMEFRAMES.items()
                )

            if not aligned:
                continue

            book = get_orderbook(item["symbol"])

            if book is None:
                continue

            pressure = (
                book["bid"]
                if item["direction"] == "LONG"
                else book["ask"]
            )

            if pressure < MIN_PRESSURE:
                continue

            item["trends"] = trends
            item["pressure"] = pressure

            item["score"] = (
                abs(item["change"])
                + sum(abs(v) for v in trends.values())
                + pressure / 20
            )

            selected.append(item)

        selected.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        report = [
            "🤖 LBank Futures Scanner",
            "📊 تحلیل روند چندبازه‌ای",
            f"بازارهای بررسی‌شده: {len(market)}",
            f"فرصت‌های اولیه: {len(candidates)}",
            f"داده تاریخی ناکافی: {missing_history}",
            f"فرصت‌های تأییدشده: {len(selected)}",
            "━━━━━━━━━━━━━━━━"
        ]

        for index, item in enumerate(selected[:MAX_RESULTS], 1):
            trends = item["trends"]

            report.extend([
                f"#{index} {item['symbol']} | {item['direction']}",
                f"قیمت: {item['price']:g}",
                f"تغییر ۲۴ ساعته: {item['change']:.2f}%",
                f"روند ۱۵ دقیقه: {trends['15m']:.2f}%",
                f"روند یک ساعت: {trends['1h']:.2f}%",
                f"روند چهار ساعت: {trends['4h']:.2f}%",
                f"فشار هم‌جهت سفارش‌ها: {item['pressure']:.1f}%",
                f"گردش مالی: {item['turnover']:,.0f}",
                "━━━━━━━━━━━━━━━━"
            ])

        if not selected:
            report.extend([
                "فرصت تأییدشده‌ای پیدا نشد.",
                "وضعیت: ورود نکن"
            ])

    except Exception as error:
        report = [
            "❌ خطا در اسکنر",
            str(error)
        ]

    finally:
        report.extend([
            "⚠️ روندها از سوابق قیمت پنج‌دقیقه‌ای محاسبه شده‌اند، نه کندل واقعی.",
            "⚠️ RSI، MACD و EMA هنوز محاسبه نمی‌شوند.",
            "⚠️ قبل از ورود حد ضرر تعیین کن.",
            "⚠️ از اهرم بالا استفاده نکن."
        ])

        text = "\n".join(report)
        REPORT_FILE.write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()