#!/usr/bin/env python3

import json
import math
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

# کندل‌ها از بازار آتی Binance دریافت می‌شوند.
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

MAX_SYMBOLS = 40
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def ema(values, period):
    if len(values) < period:
        return None

    current = sum(values[:period]) / period
    alpha = 2 / (period + 1)

    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current

    return current


def rsi(values, period=14):
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
        return 100 if avg_gain > 0 else 50

    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(values):
    if len(values) < 50:
        return None, None, None

    alpha_fast = 2 / 13
    alpha_slow = 2 / 27

    fast_value = sum(values[:12]) / 12
    slow_value = sum(values[:26]) / 26

    # تعداد خانه‌های اولیه باید دقیق باشد تا طول آرایه‌ها
    # با تعداد قیمت‌ها برابر بماند.
    fast = [None] * 12
    slow = [None] * 26

    for i in range(12, len(values)):
        fast_value = (
            alpha_fast * values[i]
            + (1 - alpha_fast) * fast_value
        )
        fast.append(fast_value)

    for i in range(26, len(values)):
        slow_value = (
            alpha_slow * values[i]
            + (1 - alpha_slow) * slow_value
        )
        slow.append(slow_value)

    line = []

    for i in range(len(values)):
        if fast[i] is None or slow[i] is None:
            line.append(None)
        else:
            line.append(fast[i] - slow[i])

    valid = [x for x in line if x is not None]

    if len(valid) < 9:
        return None, None, None

    signal = ema(valid, 9)
    current = valid[-1]

    return current, signal, current - signal


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        ranges.append(max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        ))

    return sum(ranges[-period:]) / period


def load_latest_lbank():
    if not DATA_FILE.exists():
        raise RuntimeError("LBank price history not found")

    latest = {}

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                row = json.loads(line)

                symbol = str(row.get("symbol", "")).upper()
                price = number(row.get("lastPrice"))
                turnover = number(row.get("turnover24h"))

                timestamp = datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00")
                )

                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(
                        tzinfo=timezone.utc
                    )

                if not symbol.endswith("USDT"):
                    continue

                if price is None or price <= 0:
                    continue

                old = latest.get(symbol)

                if old is None or timestamp > old["_time"]:
                    row["_time"] = timestamp
                    row["_price"] = price
                    row["_turnover"] = turnover or 0
                    latest[symbol] = row

            except Exception:
                continue

    return latest


def get_candles(symbol, interval):
    response = requests.get(
        BINANCE_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": 250
        },
        timeout=15
    )

    response.raise_for_status()
    raw = response.json()

    if not isinstance(raw, list) or len(raw) < 205:
        raise RuntimeError("Insufficient candle history")

    candles = []

    for item in raw:
        candles.append([
            int(item[0]),
            float(item[1]),
            float(item[2]),
            float(item[3]),
            float(item[4]),
            float(item[5]),
            int(item[6])
        ])

    # فقط کندل‌هایی نگه داشته می‌شوند که زمان بسته‌شدنشان
    # گذشته باشد.
    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    candles = [
        candle for candle in candles
        if candle[6] <= now_ms
    ]

    if len(candles) < 205:
        raise RuntimeError("Not enough closed candles")

    # کندل بسته‌شده معتبر را دوباره حذف نمی‌کنیم.
    return candles


def timeframe_analysis(candles):
    closes = [c[4] for c in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)

    current_rsi = rsi(closes)
    current_macd, signal, histogram = macd(closes)
    current_atr = atr(candles)

    volumes = [c[5] for c in candles]

    average_volume = sum(volumes[-21:-1]) / 20
    current_volume = volumes[-1]

    volume_ratio = (
        current_volume / average_volume
        if average_volume > 0 else 0
    )

    highs = [c[2] for c in candles[-48:]]
    lows = [c[3] for c in candles[-48:]]

    price = closes[-1]

    return {
        "price": price,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": current_rsi,
        "macd": current_macd,
        "macd_signal": signal,
        "histogram": histogram,
        "atr": current_atr,
        "volume_ratio": volume_ratio,
        "support": min(lows),
        "resistance": max(highs)
    }


def analyze_symbol(symbol, lbank):
    try:
        intervals = {
            "5m": "5m",
            "15m": "15m",
            "1h": "1h",
            "4h": "4h"
        }

        data = {}

        for name, interval in intervals.items():
            candles = get_candles(symbol, interval)
            data[name] = timeframe_analysis(candles)

        price = data["5m"]["price"]
        lbank_price = lbank["_price"]

        difference = (
            abs(price - lbank_price) / lbank_price * 100
        )

        # جلوگیری از تحلیل در صورت اختلاف زیاد قیمت دو صرافی
        if difference > PRICE_DIFF_LIMIT:
            return None

        m5 = data["5m"]
        m15 = data["15m"]
        h1 = data["1h"]
        h4 = data["4h"]

        long_trend = (
            h4["ema20"] > h4["ema50"] > h4["ema200"]
            and h1["ema20"] > h1["ema50"]
        )

        short_trend = (
            h4["ema20"] < h4["ema50"] < h4["ema200"]
            and h1["ema20"] < h1["ema50"]
        )

        long_confirm = (
            m15["ema20"] > m15["ema50"]
            and m5["ema20"] > m5["ema50"]
            and 50 <= m15["rsi"] <= 68
            and 45 <= m5["rsi"] <= 70
            and m5["histogram"] > 0
            and m15["histogram"] > 0
            and m5["volume_ratio"] >= 1.2
        )

        short_confirm = (
            m15["ema20"] < m15["ema50"]
            and m5["ema20"] < m5["ema50"]
            and 32 <= m15["rsi"] <= 50
            and 30 <= m5["rsi"] <= 55
            and m5["histogram"] < 0
            and m15["histogram"] < 0
            and m5["volume_ratio"] >= 1.2
        )

        side = None

        if long_trend and long_confirm:
            side = "LONG"

        elif short_trend and short_confirm:
            side = "SHORT"

        if side is None:
            return {
                "symbol": symbol,
                "price": price,
                "side": None,
                "rsi": m5["rsi"],
                "support": m15["support"],
                "resistance": m15["resistance"],
                "difference": difference
            }

        current_atr = m5["atr"]

        if not current_atr or current_atr <= 0:
            return None

        entry = price

        if side == "LONG":
            stop = min(
                entry - 1.5 * current_atr,
                m15["support"] - 0.2 * current_atr
            )

            risk = entry - stop

            if risk <= 0:
                return None

            targets = [
                entry + risk,
                entry + 2 * risk,
                entry + 3 * risk
            ]

            if m15["resistance"] <= entry:
                return None

            if m15["resistance"] < targets[0]:
                return None

        else:
            stop = max(
                entry + 1.5 * current_atr,
                m15["resistance"] + 0.2 * current_atr
            )

            risk = stop - entry

            if risk <= 0:
                return None

            targets = [
                entry - risk,
                entry - 2 * risk,
                entry - 3 * risk
            ]

            if m15["support"] >= entry:
                return None

            if m15["support"] > targets[0]:
                return None

        risk_percent = risk / entry * 100

        if risk_percent < 0.15 or risk_percent > 3:
            return None

        return {
            "symbol": symbol,
            "price": price,
            "side": side,
            "entry": entry,
            "stop": stop,
            "targets": targets,
            "risk_percent": risk_percent,
            "rsi": m5["rsi"],
            "volume_ratio": m5["volume_ratio"],
            "support": m15["support"],
            "resistance": m15["resistance"],
            "difference": difference,
            "turnover": lbank["_turnover"]
        }

    except Exception as error:
        print(f"{symbol}: {error}")
        return None


def fmt(value):
    if value is None:
        return "ناموجود"

    value = float(value)

    if abs(value) >= 100:
        return f"{value:,.4f}"

    if abs(value) >= 1:
        return f"{value:.6f}"

    return f"{value:.8f}"


def main():
    lbank_markets = load_latest_lbank()

    candidates = [
        (symbol, row)
        for symbol, row in lbank_markets.items()
        if row["_turnover"] >= MIN_TURNOVER
    ]

    candidates.sort(
        key=lambda x: x[1]["_turnover"],
        reverse=True
    )

    candidates = candidates[:MAX_SYMBOLS]

    results = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                analyze_symbol, symbol, row
            ): symbol
            for symbol, row in candidates
        }

        for future in as_completed(futures):
            result = future.result()

            if result:
                results.append(result)

    signals = [
        item for item in results
        if item.get("side")
    ]

    lines = [
        "📊 LBank USDT Futures Smart Scanner",
        "منبع کندل: Binance USD-M Futures",
        f"زمان: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"بازارهای LBank: {len(lbank_markets)}",
        f"بازارهای بررسی‌شده: {len(candidates)}",
        f"تحلیل‌های موفق: {len(results)}",
        ""
    ]

    if signals:
        lines.append(
            f"✅ ستاپ‌های واجد شرایط: {len(signals)}"
        )

        for item in signals[:5]:
            side = (
                "لانگ"
                if item["side"] == "LONG"
                else "شورت"
            )

            lines.extend([
                "",
                f"🔹 {item['symbol']} — {side}",
                f"ورود: {fmt(item['entry'])}",
                f"حد ضرر: {fmt(item['stop'])}",
                f"تارگت ۱: {fmt(item['targets'][0])}",
                f"تارگت ۲: {fmt(item['targets'][1])}",
                f"تارگت ۳: {fmt(item['targets'][2])}",
                f"ریسک: {item['risk_percent']:.2f}%",
                f"RSI: {item['rsi']:.2f}",
                f"حجم نسبی: {item['volume_ratio']:.2f}x",
                f"حمایت: {fmt(item['support'])}",
                f"مقاومت: {fmt(item['resistance'])}",
                f"اختلاف قیمت دو صرافی: {item['difference']:.3f}%"
            ])

    else:
        lines.extend([
            "⛔ هیچ ستاپ معتبری پیدا نشد.",
            "ورود نکن؛ منتظر تأیید بمان."
        ])

    lines.extend([
        "",
        "⚠️ کندل‌ها متعلق به Binance USD-M Futures هستند، نه LBank.",
        "⚠️ قیمت و اطلاعات قرارداد از LBank دریافت می‌شود.",
        "⚠️ سیگنال‌ها نیازمند بررسی نهایی دفتر سفارش و اجرای واقعی هستند.",
        "⚠️ اهرم بالا خطر لیکوییدشدن را افزایش می‌دهد.",
        "این ربات سفارش ثبت نمی‌کند."
    ])

    report = "\n".join(lines)

    REPORT_FILE.write_text(report, encoding="utf-8")

    print(report)


if __name__ == "__main__":
    main()