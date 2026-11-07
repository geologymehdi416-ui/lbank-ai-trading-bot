#!/usr/bin/env python3
import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==========================================
# SETTINGS
# ==========================================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OKX_URL = "https://www.okx.com/api/v5/market/candles"

MAX_SYMBOLS = 100
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75
MAX_SNAPSHOT_AGE_MINUTES = 20

MAX_RETRIES = 3
MAX_WORKERS = 5

MIN_VOLUME_RATIO = 1.2
MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H"
}

HEADERS = {
    "User-Agent": "LBank-Futures-Signal-Bot/6.0"
}


# ==========================================
# EXCEPTIONS
# ==========================================

class SkipSymbol(Exception):
    pass


# ==========================================
# UTILITIES
# ==========================================

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


# ==========================================
# INDICATORS
# ==========================================

def ema_series(values, period):

    output = [None] * len(values)

    if len(values) < period:
        return output

    current = sum(values[:period]) / period
    output[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * alpha
            + current * (1 - alpha)
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

    line = []

    for f, s in zip(fast, slow):
        if f is not None and s is not None:
            line.append(f - s)

    if len(line) < 9:
        return None, None, None

    signal_series = ema_series(line, 9)

    macd_value = line[-1]
    signal_value = signal_series[-1]

    if signal_value is None:
        return None, None, None

    histogram = macd_value - signal_value

    return macd_value, signal_value, histogram


def atr(candles, period=14):

    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):

        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        ranges.append(tr)

    return sum(ranges[-period:]) / period


# ==========================================
# LBANK DATA
# ==========================================

def load_latest_lbank():

    if not DATA_FILE.exists():
        raise RuntimeError("LBank data file not found")

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

            except Exception:
                continue

    return latest


# ==========================================
# OKX CANDLES
# ==========================================

def get_okx_symbol(symbol):

    if not symbol.endswith("USDT"):
        raise SkipSymbol("نماد USDT معتبر نیست")

    base = symbol[:-4]

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
                    "قرارداد متناظر در OKX وجود ندارد"
                )

            if code != "0":
                raise RuntimeError(
                    f"OKX error: {code} "
                    f"{payload.get('msg')}"
                )

            raw = payload.get("data", [])

            candles = []

            for item in raw:

                if len(item) < 9:
                    continue

                # فقط کندل بسته‌شده
                if str(item[8]) != "1":
                    continue

                values = [
                    number(item[1]),
                    number(item[2]),
                    number(item[3]),
                    number(item[4]),
                    number(item[5])
                ]

                if any(x is None for x in values):
                    continue

                if min(values[:4]) <= 0:
                    continue

                candles.append([
                    int(item[0]),
                    *values
                ])

            candles.sort(key=lambda x: x[0])

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
                    f"کندل کافی نیست: {len(candles)}"
                )

            interval_ms = {
                "5m": 300000,
                "15m": 900000,
                "1h": 3600000,
                "4h": 14400000
            }[interval]

            now_ms = int(
                datetime.now(timezone.utc).timestamp()
                * 1000
            )

            candle_age = (
                now_ms
                - candles[-1][0]
                - interval_ms
            )

            if candle_age > interval_ms * 2:
                raise RuntimeError(
                    "کندل آخر قدیمی است"
                )

            return candles

        except SkipSymbol:
            raise

        except Exception as error:

            last_error = error

            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(str(last_error))


# ==========================================
# TIMEFRAME ANALYSIS
# ==========================================

def timeframe_analysis(candles):

    closes = [c[4] for c in candles]

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]

    current_rsi = rsi(closes)

    macd_value, signal_value, histogram = macd(
        closes
    )

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
        c[3] for c in recent
    )

    resistance = max(
        c[2] for c in recent
    )

    required = [
        ema20,
        ema50,
        ema200,
        current_rsi,
        macd_value,
        signal_value,
        histogram,
        current_atr
    ]

    if any(x is None for x in required):
        raise RuntimeError(
            "اندیکاتور ناقص است"
        )

    return {
        "price": closes[-1],
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": current_rsi,
        "macd": macd_value,
        "macd_signal": signal_value,
        "histogram": histogram,
        "atr": current_atr,
        "volume_ratio": volume_ratio,
        "support": support,
        "resistance": resistance
    }


# ==========================================
# SIGNAL GENERATION
# ==========================================

def make_trade(side, entry, data):

    m5 = data["5m"]
    m15 = data["15m"]

    volatility = m5["atr"]

    if volatility <= 0:
        return None

    if side == "LONG":

        stop = min(
            entry - 1.5 * volatility,
            m15["support"] - 0.15 * volatility
        )

        risk = entry - stop

        if risk <= 0:
            return None

        targets = [
            entry + risk,
            entry + 2 * risk,
            entry + 3 * risk
        ]

        # مقاومت نزدیک نباید مانع تارگت اول باشد
        if m15["resistance"] < targets[0]:
            return None

    else:

        stop = max(
            entry + 1.5 * volatility,
            m15["resistance"] + 0.15 * volatility
        )

        risk = stop - entry

        if risk <= 0:
            return None

        targets = [
            entry - risk,
            entry - 2 * risk,
            entry - 3 * risk
        ]

        if m15["support"] > targets[0]:
            return None

    risk_percent = risk / entry * 100

    if not (
        MIN_RISK_PERCENT
        <= risk_percent
        <= MAX_RISK_PERCENT
    ):
        return None

    return {
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "risk_percent": risk_percent
    }


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
        abs(lbank_price - okx_price)
        / lbank_price
        * 100
    )

    if difference > PRICE_DIFF_LIMIT:
        raise SkipSymbol(
            f"اختلاف قیمت {difference:.3f}%"
        )

    # قیمت مرجع ورود از LBank گرفته می‌شود
    entry = lbank_price

    # روند تایم‌فریم بالاتر
    bullish_htf = (
        h4["ema20"] > h4["ema50"]
        and h1["ema20"] > h1["ema50"]
    )

    bearish_htf = (
        h4["ema20"] < h4["ema50"]
        and h1["ema20"] < h1["ema50"]
    )

    # تأیید تایم‌فریم پایین‌تر
    bullish_ltf = (
        m5["ema20"] > m5["ema50"]
        and m15["ema20"] > m15["ema50"]
    )

    bearish_ltf = (
        m5["ema20"] < m5["ema50"]
        and m15["ema20"] < m15["ema50"]
    )

    bullish_momentum = (
        50 <= m15["rsi"] <= 68
        and 45 <= m5["rsi"] <= 70
        and m15["histogram"] > 0
        and m5["histogram"] > 0
    )

    bearish_momentum = (
        32 <= m15["rsi"] <= 50
        and 30 <= m5["rsi"] <= 55
        and m15["histogram"] < 0
        and m5["histogram"] < 0
    )

    volume_confirmed = (
        m5["volume_ratio"] >= MIN_VOLUME_RATIO
    )

    side = None
    reasons = []

    if (
        bullish_htf
        and bullish_ltf
        and bullish_momentum
        and volume_confirmed
    ):

        side = "LONG"

    elif (
        bearish_htf
        and bearish_ltf
        and bearish_momentum
        and volume_confirmed
    ):

        side = "SHORT"

    else:

        if not volume_confirmed:
            reasons.append(
                "حجم معاملات برای ورود کافی نیست"
            )

        if not bullish_htf and not bearish_htf:
            reasons.append(
                "روند تایم‌فریم بالاتر یکپارچه نیست"
            )

        if not bullish_ltf and not bearish_ltf:
            reasons.append(
                "EMA تایم‌فریم پایین‌تر تأیید نمی‌دهد"
            )

        if not bullish_momentum and not bearish_momentum:
            reasons.append(
                "RSI و MACD مومنتوم ورود را تأیید نمی‌کنند"
            )

    trade = None

    if side:

        trade = make_trade(
            side,
            entry,
            data
        )

        if trade is None:
            reasons.append(
                "نسبت ریسک به بازده یا ساختار حمایت/مقاومت مناسب نیست"
            )
            side = None

    return {
        "symbol": symbol,
        "side": side,
        "data": data,
        "lbank_price": lbank_price,
        "okx_price": okx_price,
        "difference": difference,
        "turnover": lbank["_turnover"],
        "trade": trade,
        "reasons": reasons
    }


# ==========================================
# REPORT
# ==========================================

def build_report(
    market_count,
    candidates,
    results,
    skipped,
    failures,
    stale_count,
    now
):

    signals = [
        x for x in results
        if x["side"] in ("LONG", "SHORT")
    ]

    longs = [
        x for x in signals
        if x["side"] == "LONG"
    ]

    shorts = [
        x for x in signals
        if x["side"] == "SHORT"
    ]

    lines = [

        "🚨 LBank FUTURES AI SIGNALS",

        f"زمان: {now.strftime('%Y-%m-%d %H:%M UTC')}",

        f"بازارهای LBank: {market_count}",

        f"نمادهای منتخب: {len(candidates)}",

        f"تحلیل موفق: {len(results)}",

        f"سیگنال LONG: {len(longs)}",

        f"سیگنال SHORT: {len(shorts)}",

        f"نمادهای ردشده: {len(skipped)}",

        f"خطاهای واقعی: {len(failures)}",

        "",
        "==============================",
        "🎯 سیگنال‌های معاملاتی تأییدشده",
        "=============================="
    ]

    if not signals:

        lines.extend([
            "",
            "⛔ هیچ سیگنال معتبری پیدا نشد.",
            "ورود نکن؛ منتظر تأیید شرایط بمان."
        ])

    for item in signals:

        trade = item["trade"]
        d = item["data"]

        m5 = d["5m"]
        m15 = d["15m"]
        h1 = d["1h"]
        h4 = d["4h"]

        icon = (
            "🟢" if item["side"] == "LONG"
            else "🔴"
        )

        lines.extend([

            "",
            "━━━━━━━━━━━━━━━━",
            f"{icon} {item['side']} | {item['symbol']}",
            "━━━━━━━━━━━━━━━━",

            f"قیمت LBank: {fmt(item['lbank_price'])}",

            f"قیمت OKX: {fmt(item['okx_price'])}",

            f"اختلاف قیمت: {item['difference']:.3f}%",

            f"ورود: {fmt(trade['entry'])}",

            f"حد ضرر: {fmt(trade['stop'])}",

            f"تارگت 1: {fmt(trade['targets'][0])}",

            f"تارگت 2: {fmt(trade['targets'][1])}",

            f"تارگت 3: {fmt(trade['targets'][2])}",

            f"ریسک تا حد ضرر: {trade['risk_percent']:.2f}%",

            "",
            "📊 تحلیل تکنیکال:",

            (
                f"روند 4H: "
                f"{'صعودی' if h4['ema20'] > h4['ema50'] else 'نزولی'}"
            ),

            (
                f"روند 1H: "
                f"{'صعودی' if h1['ema20'] > h1['ema50'] else 'نزولی'}"
            ),

            f"RSI 15m: {m15['rsi']:.1f}",

            f"RSI 5m: {m5['rsi']:.1f}",

            f"MACD 15m: {fmt(m15['histogram'])}",

            f"MACD 5m: {fmt(m5['histogram'])}",

            f"حجم نسبی 5m: {m5['volume_ratio']:.2f}x",

            f"حمایت 15m: {fmt(m15['support'])}",

            f"مقاومت 15m: {fmt(m15['resistance'])}",

            "",
            "شرط اعتبار: حفظ ساختار و تأیید حجم."
        ])

    lines.extend([
        "",
        "==============================",
        "📋 وضعیت سایر نمادها",
        "=============================="
    ])

    for item in results:

        if item["side"]:
            continue

        lines.append(
            f"⚪ {item['symbol']}: بدون ورود"
        )

        if item["reasons"]:
            lines.append(
                "دلیل: "
                + "؛ ".join(item["reasons"][:3])
            )

    if skipped:

        lines.extend([
            "",
            "نمادهای ردشده:"
        ])

        for symbol, reason in skipped:
            lines.append(
                f"- {symbol}: {reason}"
            )

    if failures:

        lines.extend([
            "",
            "خطاهای واقعی:"
        ])

        for symbol, reason in failures[:20]:
            lines.append(
                f"- {symbol}: {reason[:120]}"
            )

    lines.extend([

        "",
        "⚠️ کندل‌ها از OKX Futures هستند.",

        "⚠️ قیمت ورود بر اساس قیمت LBank محاسبه شده است.",

        "⚠️ دفتر سفارش و Funding Rate بررسی نشده‌اند.",

        "⚠️ سیگنال‌ها مشروط به حفظ شرایط بازار هستند.",

        "⚠️ اهرم بالا خطر لیکوییدشدن را افزایش می‌دهد.",

        "ربات هیچ سفارشی ثبت نمی‌کند."
    ])

    return "\n".join(lines)


# ==========================================
# MAIN
# ==========================================

def main():

    now = datetime.now(timezone.utc)

    markets = load_latest_lbank()

    candidates = []
    stale_count = 0

    for symbol, row in markets.items():

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
        f"Analyzing {len(candidates)} symbols..."
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
                    f"ANALYZED {symbol}: "
                    f"{result['side'] or 'NO SIGNAL'}"
                )

            except SkipSymbol as error:

                skipped.append(
                    (symbol, str(error))
                )

            except Exception as error:

                failures.append(
                    (symbol, str(error))
                )

                print(
                    f"FAILED {symbol}: {error}"
                )

    results.sort(
        key=lambda x: x["turnover"],
        reverse=True
    )

    report = build_report(
        len(markets),
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

    print(report)


if __name__ == "__main__":
    main()