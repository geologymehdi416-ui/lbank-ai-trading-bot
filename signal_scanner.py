import json
import requests
from pathlib import Path
from datetime import datetime

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

ORDERBOOK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketOrder"

TIMEFRAMES = {
    "15m": (15, 0.05, 20),
    "1h": (60, 0.10, 30),
    "4h": (240, 0.20, 30),
}

MIN_SCORE = 70
MIN_TURNOVER = 100000
MAX_CANDIDATES = 100


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_history():
    history = {}

    if not DATA_FILE.exists():
        return history

    with open(DATA_FILE, "r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol:
                    history.setdefault(symbol, []).append(item)

            except Exception:
                continue

    for symbol in history:
        history[symbol].sort(
            key=lambda x: x.get("timestamp", "")
        )

    return history


def timeframe_score(records, minutes, threshold, weight, direction):
    if not records:
        return 0, None

    latest = records[-1]

    try:
        latest_time = datetime.fromisoformat(
            latest["timestamp"].replace("Z", "+00:00")
        )
    except Exception:
        return 0, None

    target_time = latest_time.timestamp() - minutes * 60
    old = None

    for item in records:
        try:
            item_time = datetime.fromisoformat(
                item["timestamp"].replace("Z", "+00:00")
            ).timestamp()

            if item_time <= target_time:
                old = item

        except Exception:
            continue

    if old is None:
        return 0, None

    old_price = number(old.get("lastPrice"))
    new_price = number(latest.get("lastPrice"))

    if old_price <= 0 or new_price <= 0:
        return 0, None

    change = (new_price - old_price) / old_price * 100

    directional_change = change * direction

    ratio = max(
        0,
        min(1, directional_change / threshold)
    )

    score = weight * ratio

    return score, change


def orderbook_score(symbol, direction):
    try:
        response = requests.get(
            ORDERBOOK_URL,
            params={
                "symbol": symbol,
                "depth": 20
            },
            timeout=15
        )

        response.raise_for_status()

        result = response.json()
        data = result.get("data", {})

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        bid_volume = sum(
            number(item.get("volume"))
            for item in bids
        )

        ask_volume = sum(
            number(item.get("volume"))
            for item in asks
        )

        total_volume = bid_volume + ask_volume

        if total_volume <= 0:
            return 0, 50

        bid_percent = bid_volume / total_volume * 100

        if direction == 1:
            pressure = bid_percent
        else:
            pressure = 100 - bid_percent

        score = max(
            0,
            min(20, (pressure - 45) / 20 * 20)
        )

        return score, pressure

    except Exception:
        return 0, None


def main():
    report = []

    try:
        history = load_history()

        if not history:
            raise RuntimeError("No price history available")

        candidates = []

        for symbol, records in history.items():

            if not records:
                continue

            latest = records[-1]

            price = number(latest.get("lastPrice"))
            open_price = number(latest.get("openPrice"))
            high = number(latest.get("highestPrice"))
            low = number(latest.get("lowestPrice"))
            turnover = number(latest.get("turnover24h"))

            if price <= 0 or open_price <= 0:
                continue

            if turnover < MIN_TURNOVER:
                continue

            if high <= low:
                continue

            change_24h = (
                (price - open_price) / open_price
            ) * 100

            if abs(change_24h) > 15:
                continue

            range_position = (
                (price - low) / (high - low)
            ) * 100

            if change_24h >= 1 and range_position >= 50:
                direction = 1

            elif change_24h <= -1 and range_position <= 50:
                direction = -1

            else:
                continue

            candidates.append({
                "symbol": symbol,
                "records": records,
                "direction": direction,
                "change24h": change_24h,
                "range_position": range_position
            })

        candidates.sort(
            key=lambda x: abs(x["change24h"]),
            reverse=True
        )

        ranked = []

        for candidate in candidates[:MAX_CANDIDATES]:

            total_score = 0
            details = {}

            for name, settings in TIMEFRAMES.items():

                minutes, threshold, weight = settings

                score, change = timeframe_score(
                    candidate["records"],
                    minutes,
                    threshold,
                    weight,
                    candidate["direction"]
                )

                total_score += score

                details[name] = {
                    "score": score,
                    "change": change
                }

            book_score, pressure = orderbook_score(
                candidate["symbol"],
                candidate["direction"]
            )

            total_score += book_score

            ranked.append({
                **candidate,
                "score": round(total_score, 1),
                "details": details,
                "book_score": round(book_score, 1),
                "pressure": pressure
            })

        ranked.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        qualified = [
            item for item in ranked
            if item["score"] >= MIN_SCORE
        ]

        report.append("🤖 LBank Futures Scanner")
        report.append("📊 سیستم امتیازدهی چندبازه‌ای")
        report.append(f"بازارهای دارای سابقه: {len(history)}")
        report.append(f"فرصت‌های اولیه: {len(candidates)}")
        report.append(f"نمادهای امتیازدهی‌شده: {len(ranked)}")
        report.append(f"فرصت‌های با امتیاز حداقل ۷۰: {len(qualified)}")
        report.append("━━━━━━━━━━━━━━━━")

        if qualified:

            report.append("🏆 فرصت‌های تأییدشده از نظر امتیاز:")

            display_list = qualified[:5]

        else:

            report.append("⚠️ هیچ نمادی به امتیاز ۷۰ نرسید.")
            report.append("📊 پنج نماد برتر برای بررسی:")
            report.append("این نمادها سیگنال ورود نیستند.")

            display_list = ranked[:5]

        for item in display_list:

            direction = (
                "LONG 🟢"
                if item["direction"] == 1
                else "SHORT 🔴"
            )

            report.append("")
            report.append(
                f"{item['symbol']} | {direction}"
            )

            report.append(
                f"⭐ امتیاز کل: {item['score']}/100"
            )

            report.append(
                f"تغییر ۲۴ ساعته: {item['change24h']:.2f}%"
            )

            pressure_text = (
                "نامشخص"
                if item["pressure"] is None
                else f"{item['pressure']:.1f}%"
            )

            report.append(
                f"فشار سفارش‌ها: {pressure_text}"
            )

            for tf, detail in item["details"].items():

                change = detail["change"]

                if change is None:
                    change_text = "داده ناکافی"
                else:
                    change_text = f"{change:.3f}%"

                report.append(
                    f"{tf}: امتیاز {detail['score']:.1f} "
                    f"| تغییر {change_text}"
                )

            report.append(
                f"امتیاز دفتر سفارش: {item['book_score']:.1f}/20"
            )

        if not qualified:
            report.append("")
            report.append("وضعیت معاملاتی: ورود نکن")

        report.append("")
        report.append("━━━━━━━━━━━━━━━━")
        report.append(
            "⚠️ امتیازها رتبه‌بندی الگوریتمی هستند، نه احتمال موفقیت."
        )
        report.append(
            "⚠️ روندها از سوابق قیمت پنج‌دقیقه‌ای محاسبه می‌شوند، نه کندل واقعی."
        )
        report.append(
            "⚠️ RSI، MACD و EMA در این نسخه محاسبه نمی‌شوند."
        )
        report.append(
            "⚠️ قبل از ورود، ساختار بازار و حد ضرر را بررسی کن."
        )
        report.append(
            "⚠️ از اهرم بالا استفاده نکن."
        )

    except Exception as error:

        report.append("❌ خطا در اسکن بازار")
        report.append(str(error))

    finally:

        REPORT_FILE.write_text(
            "\n".join(report),
            encoding="utf-8"
        )


if __name__ == "__main__":
    main()