#!/usr/bin/env python3
# LBank USDT Futures Smart Scanner
# Monitoring only — no automatic order execution.

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

MIN_RECORDS = 60
HISTORY_LIMIT = 300
LEVEL_WINDOW = 48
MAX_SYMBOLS = 10


def to_float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def ema_series(values, period):
    if len(values) < period:
        return []

    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period

    result = [None] * (period - 1) + [current]

    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)

    return result


def rsi_wilder(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    if avg_gain == 0:
        return 0.0

    return 100 - (100 / (1 + avg_gain / avg_loss))


def calculate_macd(values):
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    if not fast or not slow:
        return None, None, None

    macd_line = []

    for f, s in zip(fast, slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)

    valid = [x for x in macd_line if x is not None]

    signal_series = ema_series(valid, 9)

    if not signal_series:
        return None, None, None

    macd = valid[-1]
    signal = signal_series[-1]
    histogram = macd - signal

    return macd, signal, histogram


def load_history():
    history = defaultdict(list)

    if not DATA_FILE.exists():
        raise RuntimeError("Price history file not found")

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):

            try:
                row = json.loads(line)

                symbol = str(
                    row.get("symbol", "")
                ).upper()

                price = to_float(row.get("lastPrice"))

                if not symbol.endswith("USDT"):
                    continue

                if price is None or price <= 0:
                    continue

                timestamp = datetime.fromisoformat(
                    str(row["timestamp"]).replace(
                        "Z", "+00:00"
                    )
                )

                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(
                        tzinfo=timezone.utc
                    )

                row["_price"] = price
                row["_time"] = timestamp

                history[symbol].append(row)

            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                print(
                    f"Invalid record skipped: {line_number}"
                )

    result = {}

    for symbol, rows in history.items():

        unique = {
            row["_time"].isoformat(): row
            for row in rows
        }

        ordered = sorted(
            unique.values(),
            key=lambda x: x["_time"]
        )

        result[symbol] = ordered[-HISTORY_LIMIT:]

    return result


def analyze_symbol(symbol, rows):

    if len(rows) < MIN_RECORDS:
        return None

    prices = [
        row["_price"]
        for row in rows
    ]

    price = prices[-1]
    latest = rows[-1]

    ema20_series = ema_series(prices, 20)
    ema50_series = ema_series(prices, 50)
    ema200_series = ema_series(prices, 200)

    if not ema20_series or not ema50_series:
        return None

    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]

    ema200 = (
        ema200_series[-1]
        if ema200_series else None
    )

    rsi = rsi_wilder(prices)

    macd, macd_signal, macd_hist = calculate_macd(
        prices
    )

    if any(
        value is None
        for value in (
            rsi,
            macd,
            macd_signal,
            macd_hist
        )
    ):
        return None

    recent_prices = prices[-LEVEL_WINDOW:]

    support = min(recent_prices)
    resistance = max(recent_prices)

    volume = to_float(
        latest.get("volume24h")
    )

    turnover = to_float(
        latest.get("turnover24h")
    )

    funding = to_float(
        latest.get("fundingRate")
    )

    long_trend = (
        ema20 > ema50
        and (ema200 is None or price > ema200)
    )

    short_trend = (
        ema20 < ema50
        and (ema200 is None or price < ema200)
    )

    side = None
    reasons = []

    if (
        long_trend
        and price > ema20
        and 50 <= rsi <= 68
        and macd_hist > 0
    ):

        side = "LONG"

        reasons = [
            "EMA20 بالاتر از EMA50",
            "قیمت بالاتر از EMA20",
            "RSI صعودی",
            "MACD مثبت"
        ]

    elif (
        short_trend
        and price < ema20
        and 32 <= rsi <= 50
        and macd_hist < 0
    ):

        side = "SHORT"

        reasons = [
            "EMA20 پایین‌تر از EMA50",
            "قیمت پایین‌تر از EMA20",
            "RSI نزولی",
            "MACD منفی"
        ]

    plan = None

    if side == "LONG":

        stop = min(
            support * 0.998,
            price * 0.998
        )

        risk = price - stop

        risk_percent = (
            risk / price
        ) * 100

        target = min(
            price + 2 * risk,
            resistance * 0.998
        )

        reward = target - price

        rr = (
            reward / risk
            if risk > 0 else 0
        )

        if (
            0.25 <= risk_percent <= 2
            and rr >= 1.5
            and target > price
        ):

            plan = {
                "side": side,
                "entry": price,
                "stop": stop,
                "target": target,
                "rr": rr
            }

    elif side == "SHORT":

        stop = max(
            resistance * 1.002,
            price * 1.002
        )

        risk = stop - price

        risk_percent = (
            risk / price
        ) * 100

        target = max(
            price - 2 * risk,
            support * 1.002
        )

        reward = price - target

        rr = (
            reward / risk
            if risk > 0 else 0
        )

        if (
            0.25 <= risk_percent <= 2
            and rr >= 1.5
            and target < price
        ):

            plan = {
                "side": side,
                "entry": price,
                "stop": stop,
                "target": target,
                "rr": rr
            }

    return {
        "symbol": symbol,
        "price": price,
        "records": len(rows),
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "support": support,
        "resistance": resistance,
        "volume": volume,
        "turnover": turnover,
        "funding": funding,
        "side": side,
        "reasons": reasons,
        "plan": plan
    }


def fmt(value):

    if value is None:
        return "ناموجود"

    value = float(value)

    if abs(value) >= 100:
        return f"{value:,.3f}"

    if abs(value) >= 1:
        return f"{value:.5f}"

    return f"{value:.8f}"


def build_report(results, total_symbols):

    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    results.sort(
        key=lambda x: x.get("turnover") or 0,
        reverse=True
    )

    signals = [
        item for item in results
        if item["plan"] is not None
    ]

    lines = [
        "📊 LBank USDT Futures Smart Scanner",
        f"زمان: {now}",
        f"تعداد بازارها: {total_symbols}",
        f"بازارهای دارای داده کافی: {len(results)}",
        "",
        "⚠️ داده‌ها Snapshot قیمت هستند، نه کندل رسمی.",
        ""
    ]

    if signals:

        lines.append(
            f"✅ تعداد ستاپ‌های واجد شرایط: {len(signals)}"
        )

        for item in signals[:5]:

            plan = item["plan"]

            side = (
                "لانگ"
                if plan["side"] == "LONG"
                else "شورت"
            )

            lines.extend([
                "",
                f"🔹 {item['symbol']} — {side}",
                f"ورود: {fmt(plan['entry'])}",
                f"حد ضرر: {fmt(plan['stop'])}",
                f"تارگت: {fmt(plan['target'])}",
                f"ریسک به بازده: {plan['rr']:.2f}R",
                f"RSI: {item['rsi']:.2f}",
                f"EMA20: {fmt(item['ema20'])}",
                f"EMA50: {fmt(item['ema50'])}",
                f"EMA200: {fmt(item['ema200'])}",
                f"MACD: {item['macd']:.8f}",
                f"MACD Signal: {item['macd_signal']:.8f}",
                f"MACD Histogram: {item['macd_hist']:.8f}",
                f"حمایت تخمینی: {fmt(item['support'])}",
                f"مقاومت تخمینی: {fmt(item['resistance'])}",
                "دلایل: " + "، ".join(item["reasons"])
            ])

    else:

        lines.extend([
            "⛔ هیچ ستاپ معتبری پیدا نشد.",
            "ورود نکن؛ منتظر تأیید ساختار بازار بمان.",
            ""
        ])

    lines.extend([
        "",
        "🔍 برترین بازارها:"
    ])

    for item in results[:MAX_SYMBOLS]:

        lines.append(
            f"{item['symbol']} | "
            f"قیمت: {fmt(item['price'])} | "
            f"RSI: {item['rsi']:.1f} | "
            f"EMA20: {fmt(item['ema20'])} | "
            f"EMA50: {fmt(item['ema50'])} | "
            f"حجم ۲۴h: {fmt(item['volume'])}"
        )

    lines.extend([
        "",
        "📌 حمایت و مقاومت از محدوده Snapshotهای اخیر تخمین زده شده‌اند.",
        "📌 MACD از سری قیمت Snapshot محاسبه شده است.",
        "📌 حجم نمایش داده‌شده حجم ۲۴ ساعته است، نه حجم کندل.",
        "",
        "این گزارش تضمین سود یا توصیه مالی نیست.",
        "قبل از معامله، نمودار کندلی و دفتر سفارش را بررسی کن.",
        "⚠️ اهرم بالا خطر لیکوییدشدن را به‌شدت افزایش می‌دهد."
    ])

    return "\n".join(lines)[:3900]


def main():

    history = load_history()

    results = []

    for symbol, rows in history.items():

        item = analyze_symbol(
            symbol,
            rows
        )

        if item is not None:
            results.append(item)

    report = build_report(
        results,
        len(history)
    )

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print(report)
    print("Report saved successfully.")


if __name__ == "__main__":
    main()