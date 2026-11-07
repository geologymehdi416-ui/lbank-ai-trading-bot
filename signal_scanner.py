#!/usr/bin/env python3
import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# =========================
# SETTINGS
# =========================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OKX_URL = "https://www.okx.com/api/v5/market/candles"

MAX_SYMBOLS = 100
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75
MAX_SNAPSHOT_AGE_MINUTES = 20

MAX_RETRIES = 3
MAX_WORKERS = 5

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H"
}

HEADERS = {
    "User-Agent": "LBank-Futures-Monitor/5.0"
}


# =========================
# EXCEPTIONS
# =========================

class SkipSymbol(Exception):
    """نماد قابل تحلیل نیست؛ خطای اجرایی محسوب نمی‌شود."""
    pass


# =========================
# BASIC FUNCTIONS
# =========================

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fmt(value):
    if value is None:
        return "ناموجود"

    return f"{float(value):,.8g}"


# =========================
# INDICATORS
# =========================

def ema_series(values, period):

    if len(values) < period:
        return [None] * len(values)

    output = [None] * len(values)

    current = sum(values[:period]) / period
    output[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = (
            alpha * values[i]
            + (1 - alpha) * current
        )

        output[i] = current

    return output


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

    for i in range(period, len(changes)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    return 100 - (
        100 / (1 + avg_gain / avg_loss)
    )


def macd(values):

    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    macd_line = []

    for f, s in zip(fast, slow):

        if f is None or s is None:
            continue

        macd_line.append(f - s)

    if len(macd_line) < 9:
        return None, None, None

    signal_line = ema_series(macd_line, 9)

    current_macd = macd_line[-1]
    current_signal = signal_line[-1]

    if current_signal is None:
        return None, None, None

    histogram = current_macd - current_signal

    return current_macd, current_signal, histogram


def atr(candles, period=14):

    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):

        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        ranges.append(true_range)

    return sum(ranges[-period:]) / period


# =========================
# LBANK DATA
# =========================

def load_latest_lbank():

    if not DATA_FILE.exists():
        raise RuntimeError(
            "LBank price history not found"
        )

    latest = {}

    with DATA_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        for line in file:

            try:

                row = json.loads(line)

                symbol = str(
                    row.get("symbol", "")
                ).upper().strip()

                price = number(
                    row.get("lastPrice")
                )

                turnover = number(
                    row.get("turnover24h")
                )

                raw_timestamp = row.get("timestamp")

                if not raw_timestamp:
                    continue

                timestamp = datetime.fromisoformat(
                    str(raw_timestamp).replace(
                        "Z", "+00:00"
                    )
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

            except (
                ValueError,
                TypeError,
                KeyError
            ):
                continue

    return latest


# =========================
# OKX FUTURES CANDLES
# =========================

def get_okx_symbol(symbol):

    if not symbol.endswith("USDT"):
        raise SkipSymbol("نماد USDT معتبر نیست")

    base = symbol[:-4]

    if not base:
        raise SkipSymbol("نام دارایی پایه نامعتبر است")

    return f"{base}-USDT-SWAP"


def get_candles(symbol, interval):

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

            code = str(payload.get("code", ""))

            if code == "51001":
                raise SkipSymbol(
                    "قرارداد متناظر در OKX موجود نیست"
                )

            if code != "0":
                raise RuntimeError(
                    f"OKX API error: {code} - "
                    f"{payload.get('msg')}"
                )

            raw = payload.get("data", [])

            now_ms = int(
                datetime.now(timezone.utc).timestamp()
                * 1000
            )

            candles = []

            for item in raw:

                if (
                    not isinstance(item, list)
                    or len(item) < 9
                ):
                    continue

                # فقط کندل بسته‌شده
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

                values = [
                    open_price,
                    high,
                    low,
                    close,
                    volume
                ]

                if any(v is None for v in values):
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
                    volume
                ])

            candles.sort(key=lambda x: x[0])

            # حذف کندل‌های تکراری
            unique = {
                candle[0]: candle
                for candle in candles
            }

            candles = sorted(
                unique.values(),
                key=lambda x: x[0]
            )

            if len(candles) < 205:
                raise RuntimeError(
                    f"کندل بسته‌شده کافی نیست: "
                    f"{len(candles)}"
                )

            interval_ms = {
                "5m": 300000,
                "15m": 900000,
                "1h": 3600000,
                "4h": 14400000
            }[interval]

            candle_age = now_ms - (
                candles[-1][0] + interval_ms
            )

            if candle_age > interval_ms * 2:
                raise RuntimeError(
                    "آخرین کندل OKX قدیمی است"
                )

            return candles

        except SkipSymbol:
            raise

        except Exception as error:

            last_error = error

            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(
        f"خطای دریافت کندل {inst_id}: {last_error}"
    )


# =========================
# TIMEFRAME ANALYSIS
# =========================

def timeframe_analysis(candles):

    closes = [c[4] for c in candles]

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]

    current_rsi = rsi(closes)

    current_macd, signal, histogram = macd(closes)

    current_atr = atr(candles)

    volumes = [c[5] for c in candles]

    average_volume = (
        sum(volumes[-21:-1]) / 20
    )

    volume_ratio = (
        volumes[-1] / average_volume
        if average_volume > 0
        else 0
    )

    recent = candles[-48:]

    support = min(
        candle[3] for candle in recent
    )

    resistance = max(
        candle[2] for candle in recent
    )

    required = [
        ema20,
        ema50,
        ema200,
        current_rsi,
        current_macd,
        signal,
        histogram,
        current_atr
    ]

    if any(x is None for x in required):
        raise RuntimeError(
            "محاسبه اندیکاتورها ناموفق بود"
        )

    return {
        "price": closes[-1],
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": current_rsi,
        "macd": current_macd,
        "macd_signal": signal,
        "histogram": histogram,
        "atr": current_atr,
        "volume_ratio": volume_ratio,
        "support": support,
        "resistance": resistance
    }


# =========================
# SYMBOL ANALYSIS
# =========================

def analyze_symbol(symbol, lbank):

    data = {}

    for interval in INTERVALS:

        candles = get_candles(
            symbol,
            interval
        )

        data[interval] = timeframe_analysis(
            candles
        )

    m5 = data["5m"]
    m15 = data["15m"]
    h1 = data["1h"]
    h4 = data["4h"]

    lbank_price = lbank["_price"]
    okx_price = m5["price"]

    difference = (
        abs(okx_price - lbank_price)
        / lbank_price
        * 100
    )

    if difference > PRICE_DIFF_LIMIT:

        raise SkipSymbol(
            f"اختلاف قیمت دو صرافی: "
            f"{difference:.3f}%"
        )

    # روند تایم‌فریم بالاتر

    long_trend = (
        h4["ema20"] > h4["ema50"] > h4["ema200"]
        and h1["ema20"] > h1["ema50"]
    )

    short_trend = (
        h4["ema20"] < h4["ema50"] < h4["ema200"]
        and h1["ema20"] < h1["ema50"]
    )

    # شرایط لانگ

    long_checks = [

        (
            "روند 4h و 1h صعودی نیست",
            long_trend
        ),

        (
            "EMA در 5m و 15m صعودی نیست",
            (
                m5["ema20"] > m5["ema50"]
                and m15["ema20"] > m15["ema50"]
            )
        ),

        (
            "RSI برای لانگ مناسب نیست",
            (
                50 <= m15["rsi"] <= 68
                and 45 <= m5["rsi"] <= 70
            )
        ),

        (
            "MACD مثبت نیست",
            (
                m5["histogram"] > 0
                and m15["histogram"] > 0
            )
        ),

        (
            "حجم معاملات کافی نیست",
            m5["volume_ratio"] >= 1.2
        )
    ]

    # شرایط شورت

    short_checks = [

        (
            "روند 4h و 1h نزولی نیست",
            short_trend
        ),

        (
            "EMA در 5m و 15m نزولی نیست",
            (
                m5["ema20"] < m5["ema50"]
                and m15["ema20"] < m15["ema50"]
            )
        ),

        (
            "RSI برای شورت مناسب نیست",
            (
                32 <= m15["rsi"] <= 50
                and 30 <= m5["rsi"] <= 55
            )
        ),

        (
            "MACD منفی نیست",
            (
                m5["histogram"] < 0
                and m15["histogram"] < 0
            )
        ),

        (
            "حجم معاملات کافی نیست",
            m5["volume_ratio"] >= 1.2
        )
    ]

    side = None
    reasons = []

    if all(ok for _, ok in long_checks):

        side = "LONG"

    elif all(ok for _, ok in short_checks):

        side = "SHORT"

    else:

        reasons = [
            "لانگ: " + name
            for name, ok in long_checks
            if not ok
        ]

        reasons += [
            "شورت: " + name
            for name, ok in short_checks
            if not ok
        ]

    trade = None

    if side:

        entry = okx_price
        current_atr = m5["atr"]

        if current_atr <= 0:
            raise RuntimeError("ATR نامعتبر است")

        if side == "LONG":

            stop = min(
                entry - 1.5 * current_atr,
                m15["support"] - 0.2 * current_atr
            )

            risk = entry - stop

            if (
                risk <= 0
                or m15["resistance"] <= entry
                or m15["resistance"] < entry + risk
            ):

                side = None

            else:

                targets = [
                    entry + risk,
                    entry + 2 * risk,
                    entry + 3 * risk
                ]

        else:

            stop = max(
                entry + 1.5 * current_atr,
                m15["resistance"] + 0.2 * current_atr
            )

            risk = stop - entry

            if (
                risk <= 0
                or m15["support"] >= entry
                or m15["support"] > entry - risk
            ):

                side = None

            else:

                targets = [
                    entry - risk,
                    entry - 2 * risk,
                    entry - 3 * risk
                ]

        if side:

            risk_percent = risk / entry * 100

            if (
                risk_percent < 0.15
                or risk_percent > 3
            ):

                side = None

            else:

                trade = {
                    "entry": entry,
                    "stop": stop,
                    "targets": targets,
                    "risk_percent": risk_percent
                }

        if side is None:

            reasons.append(
                "ساختار حمایت و مقاومت یا حد ضرر مناسب نیست"
            )

    return {
        "symbol": symbol,
        "side": side,
        "data": data,
        "lbank_price": lbank_price,
        "okx_price": okx_price,
        "difference": difference,
        "turnover": lbank["_turnover"],
        "reasons": reasons,
        "trade": trade
    }


# =========================
# REPORT GENERATION
# =========================

def build_report(
    lbank_markets,
    candidates,
    results,
    skipped,
    failures,
    stale_count,
    now
):

    signals = [
        x for x in results
        if x["side"]
    ]

    lines = [

        "📊 LBank USDT Futures Smart Scanner",

        "منبع کندل: OKX USDT Perpetual Futures",

        f"زمان: {now.strftime('%Y-%m-%d %H:%M UTC')}",

        f"بازارهای LBank: {len(lbank_markets)}",

        f"داده‌های قدیمی: {stale_count}",

        f"نمادهای منتخب: {len(candidates)}",

        f"تحلیل موفق: {len(results)}",

        f"نمادهای ردشده: {len(skipped)}",

        f"خطاهای واقعی: {len(failures)}",

        f"ستاپ‌های تأییدشده: {len(signals)}",

        "",

        "📋 تحلیل تک‌تک نمادها"
    ]

    for item in results:

        d = item["data"]

        m5 = d["5m"]
        m15 = d["15m"]
        h1 = d["1h"]
        h4 = d["4h"]

        if (
            h4["ema20"] > h4["ema50"]
            > h4["ema200"]
        ):

            trend = "صعودی"

        elif (
            h4["ema20"] < h4["ema50"]
            < h4["ema200"]
        ):

            trend = "نزولی"

        else:

            trend = "خنثی/ترکیبی"

        if item["side"] == "LONG":
            label = "🟢 LONG"

        elif item["side"] == "SHORT":
            label = "🔴 SHORT"

        else:
            label = "⚪ بدون ورود"

        lines.extend([

            "",

            f"🔹 {item['symbol']} | {label}",

            (
                f"قیمت LBank: {fmt(item['lbank_price'])}"
                f" | OKX: {fmt(item['okx_price'])}"
            ),

            f"اختلاف قیمت: {item['difference']:.3f}%",

            f"روند 4h: {trend}",

            (
                f"EMA 4h: "
                f"{fmt(h4['ema20'])} / "
                f"{fmt(h4['ema50'])} / "
                f"{fmt(h4['ema200'])}"
            ),

            (
                f"EMA 1h: "
                f"{fmt(h1['ema20'])} / "
                f"{fmt(h1['ema50'])}"
            ),

            (
                f"RSI 15m / 5m: "
                f"{m15['rsi']:.1f} / "
                f"{m5['rsi']:.1f}"
            ),

            (
                f"MACD Hist 15m / 5m: "
                f"{fmt(m15['histogram'])} / "
                f"{fmt(m5['histogram'])}"
            ),

            (
                f"حجم نسبی 5m: "
                f"{m5['volume_ratio']:.2f}x"
            ),

            (
                f"حمایت 15m: {fmt(m15['support'])}"
                f" | مقاومت: {fmt(m15['resistance'])}"
            )
        ])

        if item["trade"]:

            trade = item["trade"]

            lines.extend([

                f"ورود: {fmt(trade['entry'])}",

                f"حد ضرر: {fmt(trade['stop'])}",

                (
                    f"ریسک: "
                    f"{trade['risk_percent']:.2f}%"
                ),

                f"TP1: {fmt(trade['targets'][0])}",

                f"TP2: {fmt(trade['targets'][1])}",

                f"TP3: {fmt(trade['targets'][2])}"
            ])

        elif item["reasons"]:

            lines.append(
                "دلایل عدم ورود: "
                + "؛ ".join(item["reasons"][:4])
            )

    if skipped:

        lines.extend([
            "",
            "ℹ️ نمادهای ردشده:"
        ])

        for symbol, reason in skipped:

            lines.append(
                f"- {symbol}: {reason}"
            )

    if failures:

        lines.extend([
            "",
            "⚠️ خطاهای واقعی دریافت یا تحلیل:"
        ])

        for symbol, reason in failures[:20]:

            lines.append(
                f"- {symbol}: {reason[:150]}"
            )

    if not signals:

        lines.extend([
            "",
            "⛔ ستاپ معتبر تأیید نشد.",
            "ورود نکن؛ منتظر تأیید شرایط بمان."
        ])

    lines.extend([

        "",

        "⚠️ کندل‌ها از OKX Futures هستند، نه LBank.",

        "⚠️ قیمت LBank با قیمت OKX مقایسه می‌شود.",

        "⚠️ دفتر سفارش و Funding Rate بررسی نمی‌شوند.",

        "⚠️ فقط نمادهای منتخب بر اساس حجم معاملات بررسی شده‌اند.",

        "⚠️ حد ضرر و تارگت‌ها تخمینی هستند.",

        "⚠️ اهرم بالا خطر لیکوییدشدن را افزایش می‌دهد.",

        "این ربات سفارش ثبت نمی‌کند."
    ])

    return "\n".join(lines)


# =========================
# MAIN
# =========================

def main():

    now = datetime.now(timezone.utc)

    lbank_markets = load_latest_lbank()

    candidates = []
    stale_count = 0

    for symbol, row in lbank_markets.items():

        age = (
            now - row["_time"]
        ).total_seconds() / 60

        if (
            age < -2
            or age > MAX_SNAPSHOT_AGE_MINUTES
        ):

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

    print(
        f"Starting analysis of "
        f"{len(candidates)} markets..."
    )

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

                results.append(result)

                print(
                    f"ANALYZED: {symbol}"
                )

            except SkipSymbol as error:

                skipped.append(
                    (symbol, str(error))
                )

                print(
                    f"SKIPPED: {symbol} - {error}"
                )

            except Exception as error:

                failures.append(
                    (symbol, str(error))
                )

                print(
                    f"FAILED: {symbol} - {error}"
                )

    results.sort(
        key=lambda x: x["turnover"],
        reverse=True
    )

    report = build_report(
        lbank_markets,
        candidates,
        results,
        skipped,
        failures,
        stale_count,
        now
    )

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print("\n")
    print(report)


if __name__ == "__main__":
    main()