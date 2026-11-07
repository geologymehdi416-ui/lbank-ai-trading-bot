#!/usr/bin/env python3

import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

# OKX USDT Perpetual Futures
OKX_URL = "https://www.okx.com/api/v5/market/candles"

MAX_SYMBOLS = 40
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75
MAX_SNAPSHOT_AGE_MINUTES = 20
MAX_RETRIES = 3
MAX_WORKERS = 6

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H"
}

HEADERS = {
    "User-Agent": "LBank-Futures-Monitor/4.1"
}


class SkipSymbol(Exception):
    """نماد قابل بررسی نیست؛ این مورد خطای اجرایی محسوب نمی‌شود."""
    pass


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
    if len(values) < 35:
        return None, None, None

    fast = sum(values[:12]) / 12
    slow = sum(values[:26]) / 26

    fast_values = [None] * 11 + [fast]
    slow_values = [None] * 25 + [slow]

    alpha_fast = 2 / 13
    alpha_slow = 2 / 27

    for value in values[12:]:
        fast = alpha_fast * value + (1 - alpha_fast) * fast
        fast_values.append(fast)

    for value in values[26:]:
        slow = alpha_slow * value + (1 - alpha_slow) * slow
        slow_values.append(slow)

    macd_values = [
        fast_values[i] - slow_values[i]
        for i in range(len(values))
        if fast_values[i] is not None
        and slow_values[i] is not None
    ]

    if len(macd_values) < 9:
        return None, None, None

    signal = ema(macd_values, 9)
    current = macd_values[-1]

    if signal is None:
        return None, None, None

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

                symbol = str(
                    row.get("symbol", "")
                ).upper()

                price = number(row.get("lastPrice"))
                turnover = number(row.get("turnover24h"))

                raw_timestamp = row.get("timestamp")

                if not raw_timestamp:
                    continue

                timestamp = datetime.fromisoformat(
                    str(raw_timestamp).replace("Z", "+00:00")
                )

                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(
                        tzinfo=timezone.utc
                    )

                if (
                    not symbol.endswith("USDT")
                    or not price
                    or price <= 0
                ):
                    continue

                old = latest.get(symbol)

                if old is None or timestamp > old["_time"]:
                    row["_time"] = timestamp
                    row["_price"] = price
                    row["_turnover"] = turnover or 0

                    latest[symbol] = row

            except (ValueError, TypeError, KeyError):
                continue

    return latest


def get_okx_symbol(symbol):
    if not symbol.endswith("USDT"):
        raise SkipSymbol(f"Invalid USDT symbol: {symbol}")

    base = symbol[:-4]

    if not base:
        raise SkipSymbol(f"Invalid base asset: {symbol}")

    return f"{base}-USDT-SWAP"


def get_candles(symbol, interval):
    if interval not in INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")

    inst_id = get_okx_symbol(symbol)

    params = {
        "instId": inst_id,
        "bar": INTERVALS[interval],
        "limit": "250"
    }

    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                OKX_URL,
                params=params,
                headers=HEADERS,
                timeout=20
            )

            response.raise_for_status()

            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError("Invalid OKX response")

            code = str(payload.get("code", ""))

            # قرارداد متناظر در OKX وجود ندارد.
            # این وضعیت را دوباره امتحان نمی‌کنیم.
            if code == "51001":
                raise SkipSymbol(
                    f"OKX contract unavailable: {inst_id}"
                )

            if code != "0":
                raise RuntimeError(
                    f"OKX API error: "
                    f"{code} - {payload.get('msg')}"
                )

            raw = payload.get("data", [])

            if not isinstance(raw, list):
                raise RuntimeError("Invalid OKX candle data")

            now_ms = int(
                datetime.now(timezone.utc).timestamp() * 1000
            )

            candles = []

            for item in raw:

                if not isinstance(item, list) or len(item) < 9:
                    continue

                # فقط کندل‌های بسته‌شده
                if str(item[8]) != "1":
                    continue

                try:
                    start_ms = int(item[0])
                except (TypeError, ValueError):
                    continue

                open_price = number(item[1])
                high = number(item[2])
                low = number(item[3])
                close = number(item[4])
                volume = number(item[5])

                if any(
                    value is None
                    for value in (
                        open_price,
                        high,
                        low,
                        close,
                        volume
                    )
                ):
                    continue

                if min(
                    open_price,
                    high,
                    low,
                    close
                ) <= 0:
                    continue

                if volume < 0:
                    continue

                candles.append([
                    start_ms,
                    open_price,
                    high,
                    low,
                    close,
                    volume,
                    start_ms
                ])

            # مرتب‌سازی از قدیمی به جدید
            candles.sort(key=lambda x: x[0])

            # حذف کندل تکراری احتمالی
            unique = {}

            for candle in candles:
                unique[candle[0]] = candle

            candles = sorted(
                unique.values(),
                key=lambda x: x[0]
            )

            if len(candles) < 205:
                raise RuntimeError(
                    f"Insufficient closed candles for "
                    f"{inst_id}: {len(candles)}"
                )

            # کنترل تازگی آخرین کندل
            latest_start = candles[-1][0]

            interval_ms = {
                "5m": 5 * 60 * 1000,
                "15m": 15 * 60 * 1000,
                "1h": 60 * 60 * 1000,
                "4h": 4 * 60 * 60 * 1000
            }[interval]

            candle_age = now_ms - (
                latest_start + interval_ms
            )

            if candle_age > interval_ms * 2:
                raise RuntimeError(
                    f"Stale OKX candles for {inst_id}"
                )

            return candles

        except SkipSymbol:
            # نماد نامعتبر یا فاقد قرارداد است؛ retry نکن.
            raise

        except Exception as error:

            last_error = error

            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(
        f"OKX candle error for {inst_id}: {last_error}"
    )


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

    volume_ratio = (
        volumes[-1] / average_volume
        if average_volume > 0 else 0
    )

    highs = [c[2] for c in candles[-48:]]
    lows = [c[3] for c in candles[-48:]]

    values = [
        e20,
        e50,
        e200,
        current_rsi,
        current_macd,
        signal,
        histogram,
        current_atr
    ]

    if any(value is None for value in values):
        raise RuntimeError("Indicator calculation failed")

    return {
        "price": closes[-1],
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

    data = {
        tf: timeframe_analysis(
            get_candles(symbol, tf)
        )
        for tf in INTERVALS
    }

    price = data["5m"]["price"]
    lbank_price = lbank["_price"]

    difference = (
        abs(price - lbank_price) / lbank_price * 100
    )

    # اختلاف قیمت بالا یعنی احتمالاً دو بازار
    # یا دو قرارداد معادل نیستند.
    if difference > PRICE_DIFF_LIMIT:
        raise SkipSymbol(
            f"Price difference too high: {difference:.3f}%"
        )

    m5 = data["5m"]
    m15 = data["15m"]
    h1 = data["1h"]
    h4 = data["4h"]

    # روند تایم‌فریم بالاتر
    long_trend = (
        h4["ema20"] > h4["ema50"] > h4["ema200"]
        and h1["ema20"] > h1["ema50"]
    )

    short_trend = (
        h4["ema20"] < h4["ema50"] < h4["ema200"]
        and h1["ema20"] < h1["ema50"]
    )

    # تأیید لانگ
    long_confirm = (
        m15["ema20"] > m15["ema50"]
        and m5["ema20"] > m5["ema50"]
        and 50 <= m15["rsi"] <= 68
        and 45 <= m5["rsi"] <= 70
        and m5["histogram"] > 0
        and m15["histogram"] > 0
        and m5["volume_ratio"] >= 1.2
    )

    # تأیید شورت
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
            "side": None
        }

    current_atr = m5["atr"]

    if not current_atr or current_atr <= 0:
        raise RuntimeError("Invalid ATR")

    entry = price

    if side == "LONG":

        stop = min(
            entry - 1.5 * current_atr,
            m15["support"] - 0.2 * current_atr
        )

        risk = entry - stop

        if risk <= 0 or m15["resistance"] <= entry:
            raise RuntimeError("Invalid long structure")

        targets = [
            entry + risk,
            entry + 2 * risk,
            entry + 3 * risk
        ]

        if m15["resistance"] < targets[0]:
            raise RuntimeError(
                "Insufficient resistance clearance"
            )

    else:

        stop = max(
            entry + 1.5 * current_atr,
            m15["resistance"] + 0.2 * current_atr
        )

        risk = stop - entry

        if risk <= 0 or m15["support"] >= entry:
            raise RuntimeError("Invalid short structure")

        targets = [
            entry - risk,
            entry - 2 * risk,
            entry - 3 * risk
        ]

        if m15["support"] > targets[0]:
            raise RuntimeError(
                "Insufficient support clearance"
            )

    risk_percent = risk / entry * 100

    if risk_percent < 0.15 or risk_percent > 3:
        raise RuntimeError("Risk outside permitted range")

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

    now = datetime.now(timezone.utc)

    candidates = []
    stale_count = 0

    for symbol, row in lbank_markets.items():

        age = (
            now - row["_time"]
        ).total_seconds() / 60

        # داده‌های خیلی قدیمی یا دارای timestamp آینده
        # وارد اسکن نمی‌شوند.
        if age < -2 or age > MAX_SNAPSHOT_AGE_MINUTES:
            stale_count += 1
            continue

        if row["_turnover"] >= MIN_TURNOVER:
            candidates.append((symbol, row))

    candidates.sort(
        key=lambda x: x[1]["_turnover"],
        reverse=True
    )

    candidates = candidates[:MAX_SYMBOLS]

    results = []
    skipped = []
    failures = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
                row
            ): symbol

            for symbol, row in candidates
        }

        for future in as_completed(futures):

            symbol = futures[future]

            try:
                result = future.result()

                if result:
                    results.append(result)

            except SkipSymbol as error:
                skipped.append((symbol, str(error)))

            except Exception as error:
                failures.append((symbol, str(error)))

    signals = [
        item for item in results
        if item.get("side")
    ]

    lines = [
        "📊 LBank USDT Futures Smart Scanner",
        "منبع کندل: OKX USDT Perpetual Futures",
        f"زمان: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"بازارهای LBank: {len(lbank_markets)}",
        f"بازارهای قدیمی یا نامعتبر: {stale_count}",
        f"نمادهای انتخاب‌شده برای بررسی: {len(candidates)}",
        f"تحلیل‌های موفق: {len(results)}",
        f"نمادهای ردشده: {len(skipped)}",
        f"خطاهای واقعی دریافت/تحلیل: {len(failures)}",
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

    # دلایل رد شدن نمادها
    if skipped:

        skip_reasons = {}

        for _, message in skipped:

            if "Price difference too high" in message:
                key = "اختلاف قیمت بیش از حد مجاز"

            elif "OKX contract unavailable" in message:
                key = "قرارداد متناظر در OKX موجود نیست"

            else:
                key = message[:120]

            skip_reasons[key] = (
                skip_reasons.get(key, 0) + 1
            )

        lines.extend([
            "",
            "ℹ️ نمادهای ردشده:"
        ])

        for reason, count in list(skip_reasons.items())[:5]:
            lines.append(f"- {count} مورد: {reason}")

    # خطاهای واقعی دریافت یا تحلیل
    if failures:

        lines.extend([
            "",
            "⚠️ اسکن ناقص است؛ بعضی نمادها خطای واقعی داشته‌اند."
        ])

        if not signals:
            lines.extend([
                "نبود سیگنال در این اجرا تأیید نشده است.",
                "ورود نکن تا دریافت داده کامل شود."
            ])

        sample_errors = {}

        for _, message in failures:
            key = message[:160]
            sample_errors[key] = (
                sample_errors.get(key, 0) + 1
            )

        lines.extend([
            "",
            "نمونه خطاهای واقعی:"
        ])

        for message, count in list(
            sample_errors.items()
        )[:3]:

            lines.append(
                f"- {count} مورد: {message}"
            )

    elif not signals:

        lines.extend([
            "",
            "⛔ در نمادهای قابل تحلیل، ستاپ معتبری پیدا نشد.",
            "ورود نکن؛ منتظر تأیید شرایط بمان."
        ])

    lines.extend([
        "",
        "⚠️ کندل‌ها از OKX Futures هستند، نه LBank.",
        "⚠️ قیمت LBank با قیمت OKX مقایسه می‌شود.",
        "⚠️ دفتر سفارش و Funding Rate بررسی نمی‌شوند.",
        "⚠️ فقط نمادهای انتخاب‌شده بر اساس حجم معاملات بررسی شده‌اند.",
        "⚠️ اهرم بالا خطر لیکوییدشدن را افزایش می‌دهد.",
        "این ربات سفارش ثبت نمی‌کند."
    ])

    report = "\n".join(lines)

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print(report)


if __name__ == "__main__":
    main()