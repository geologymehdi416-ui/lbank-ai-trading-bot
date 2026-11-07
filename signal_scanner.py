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

MAX_SYMBOLS = 30
MIN_TURNOVER = 100000
PRICE_DIFF_LIMIT = 0.75
MAX_SNAPSHOT_AGE_MINUTES = 20

MAX_RETRIES = 3
MAX_WORKERS = 5

MIN_VOLUME_RATIO = 1.2
MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0

# حداقل امتیاز برای نمایش در واچ‌لیست
MIN_WATCH_SCORE = 45

# تعداد فرصت‌های هر سمت در گزارش
MAX_WATCH_PER_SIDE = 8

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H"
}

HEADERS = {
    "User-Agent": "LBank-Futures-Signal-Bot/7.0"
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


def clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


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
# WATCHLIST SCORING
# ==========================================

def calculate_watch_scores(data):

    """
    امتیاز واچ‌لیست مستقل از سیگنال قطعی است.
    امتیاز بالا به معنی تضمین سود یا سیگنال ورود نیست.
    """

    long_score = 0
    short_score = 0

    weights = {
        "4h": 20,
        "1h": 20,
        "15m": 15,
        "5m": 10
    }

    for timeframe, weight in weights.items():

        item = data[timeframe]

        if (
            item["ema20"] > item["ema50"]
            and item["ema50"] > item["ema200"]
        ):
            long_score += weight

        elif (
            item["ema20"] < item["ema50"]
            and item["ema50"] < item["ema200"]
        ):
            short_score += weight

        elif item["ema20"] > item["ema50"]:
            long_score += weight * 0.5

        elif item["ema20"] < item["ema50"]:
            short_score += weight * 0.5

    m5 = data["5m"]
    m15 = data["15m"]

    # مومنتوم MACD
    if m15["histogram"] > 0:
        long_score += 8
    elif m15["histogram"] < 0:
        short_score += 8

    if m5["histogram"] > 0:
        long_score += 5
    elif m5["histogram"] < 0:
        short_score += 5

    # RSI؛ از امتیازدهی به وضعیت اشباع شدید پرهیز می‌شود
    if 50 <= m15["rsi"] <= 68:
        long_score += 8
    elif 32 <= m15["rsi"] <= 50:
        short_score += 8

    if 50 <= m5["rsi"] <= 70:
        long_score += 5
    elif 30 <= m5["rsi"] <= 50:
        short_score += 5

    # حجم نسبی
    if m5["volume_ratio"] >= MIN_VOLUME_RATIO:
        if m5["histogram"] > 0:
            long_score += 8
        elif m5["histogram"] < 0:
            short_score += 8

    return (
        round(clamp(long_score)),
        round(clamp(short_score))
    )


# ==========================================
# TRADE PLAN
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


# ==========================================
# SIGNAL ANALYSIS
# ==========================================

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

    # قیمت مرجع برنامه از LBank
    entry = lbank_price

    bullish_htf = (
        h4["ema20"] > h4["ema50"]
        and h1["ema20"] > h1["ema50"]
    )

    bearish_htf = (
        h4["ema20"] < h4["ema50"]
        and h1["ema20"] < h1["ema50"]
    )

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

    long_score, short_score = calculate_watch_scores(data)

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
                "ریسک یا ساختار حمایت/مقاومت مناسب نیست"
            )
            side = None

    # انتخاب سمت برتر برای واچ‌لیست
    watch_side = (
        "LONG" if long_score >= short_score
        else "SHORT"
    )

    watch_score = max(long_score, short_score)

    watch_trade = None

    if watch_score >= MIN_WATCH_SCORE:

        watch_trade = make_trade(
            watch_side,
            entry,
            data
        )

    # شرط فعال‌سازی؛ هنوز به معنی ورود قطعی نیست
    if watch_side == "LONG":

        activation = (
            "بسته‌شدن کندل 5m بالای EMA20، "
            "حفظ EMA20 و حجم نسبی حداقل 1.2x"
        )

    else:

        activation = (
            "بسته‌شدن کندل 5m زیر EMA20، "
            "حفظ فشار نزولی و حجم نسبی حداقل 1.2x"
        )

    return {
        "symbol": symbol,
        "side": side,
        "data": data,
        "lbank_price": lbank_price,
        "okx_price": okx_price,
        "difference": difference,
        "turnover": lbank["_turnover"],
        "trade": trade,
        "reasons": reasons,
        "long_score": long_score,
        "short_score": short_score,
        "watch_side": watch_side,
        "watch_score": watch_score,
        "watch_trade": watch_trade,
        "activation": activation
    }
# ==========================================
# REPORT HELPERS
# ==========================================

def trend_text(item):

    if (
        item["ema20"] > item["ema50"]
        and item["ema50"] > item["ema200"]
    ):
        return "صعودی قوی"

    if (
        item["ema20"] < item["ema50"]
        and item["ema50"] < item["ema200"]
    ):
        return "نزولی قوی"

    if item["ema20"] > item["ema50"]:
        return "تمایل صعودی"

    if item["ema20"] < item["ema50"]:
        return "تمایل نزولی"

    return "خنثی"


def indicator_lines(data):

    lines = []

    for timeframe in ("4h", "1h", "15m", "5m"):

        item = data[timeframe]

        lines.extend([
            f"--- تایم‌فریم {timeframe} ---",
            f"روند EMA: {trend_text(item)}",
            f"قیمت کندل: {fmt(item['price'])}",
            f"EMA20: {fmt(item['ema20'])}",
            f"EMA50: {fmt(item['ema50'])}",
            f"EMA200: {fmt(item['ema200'])}",
            f"RSI: {item['rsi']:.1f}",
            f"MACD: {fmt(item['macd'])}",
            f"MACD Signal: {fmt(item['macd_signal'])}",
            f"MACD Histogram: {fmt(item['histogram'])}",
            f"ATR: {fmt(item['atr'])}",
            f"حجم نسبی: {item['volume_ratio']:.2f}x",
            f"حمایت: {fmt(item['support'])}",
            f"مقاومت: {fmt(item['resistance'])}",
            ""
        ])

    return lines


def append_trade_plan(lines, trade):

    if not trade:
        lines.append(
            "پلن Entry/SL/TP معتبر نیست؛ فعلاً ورود نکن."
        )
        return

    lines.extend([
        f"Entry پیشنهادی: {fmt(trade['entry'])}",
        f"Stop Loss: {fmt(trade['stop'])}",
        f"Take Profit 1: {fmt(trade['targets'][0])}",
        f"Take Profit 2: {fmt(trade['targets'][1])}",
        f"Take Profit 3: {fmt(trade['targets'][2])}",
        f"فاصله ریسک تا SL: {trade['risk_percent']:.2f}%",
    ])


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

    watch_longs = sorted(
        [
            x for x in results
            if (
                x["side"] is None
                and x["watch_side"] == "LONG"
                and x["watch_score"] >= MIN_WATCH_SCORE
            )
        ],
        key=lambda x: x["watch_score"],
        reverse=True
    )[:MAX_WATCH_PER_SIDE]

    watch_shorts = sorted(
        [
            x for x in results
            if (
                x["side"] is None
                and x["watch_side"] == "SHORT"
                and x["watch_score"] >= MIN_WATCH_SCORE
            )
        ],
        key=lambda x: x["watch_score"],
        reverse=True
    )[:MAX_WATCH_PER_SIDE]

    lines = [

        "🚨 LBank FUTURES AI SCANNER",

        f"زمان: {now.strftime('%Y-%m-%d %H:%M UTC')}",

        f"کل بازارهای LBank: {market_count}",

        f"نمادهای منتخب: {len(candidates)} از حداکثر {MAX_SYMBOLS}",

        f"تحلیل موفق: {len(results)}",

        f"سیگنال قطعی LONG: {len(longs)}",

        f"سیگنال قطعی SHORT: {len(shorts)}",

        f"واچ‌لیست LONG: {len(watch_longs)}",

        f"واچ‌لیست SHORT: {len(watch_shorts)}",

        f"نمادهای ردشده: {len(skipped)}",

        f"خطاهای واقعی: {len(failures)}",

        f"داده‌های قدیمی یا نامعتبر: {stale_count}",

        "",
        "================================",
        "🎯 سیگنال‌های قطعی معاملاتی",
        "================================"
    ]

    if not signals:

        lines.extend([
            "",
            "⛔ هیچ سیگنال قطعی پیدا نشد.",
            "ورود نکن؛ منتظر تأیید شرایط بمان."
        ])

    for item in signals:

        trade = item["trade"]
        data = item["data"]

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
            f"امتیاز LONG: {item['long_score']}/100",
            f"امتیاز SHORT: {item['short_score']}/100",
        ])

        append_trade_plan(lines, trade)

        lines.extend([
            "",
            "📊 شاخص‌های تکنیکال:",
            *indicator_lines(data),
            "وضعیت: سیگنال قطعی طبق فیلترهای فعلی.",
            "قبل از ورود، قیمت و نقدشوندگی قرارداد LBank را بررسی کن."
        ])

    # ======================================
    # LONG WATCHLIST
    # ======================================

    lines.extend([
        "",
        "================================",
        "🟢 فرصت‌های تحت‌نظر LONG",
        "================================",
        "این موارد سیگنال قطعی نیستند."
    ])

    if not watch_longs:
        lines.append(
            "فرصت LONG با امتیاز کافی پیدا نشد."
        )

    for rank, item in enumerate(watch_longs, 1):

        lines.extend([
            "",
            f"#{rank} | {item['symbol']}",
            f"امتیاز LONG: {item['long_score']}/100",
            f"امتیاز SHORT: {item['short_score']}/100",
            f"قیمت LBank: {fmt(item['lbank_price'])}",
            f"اختلاف قیمت: {item['difference']:.3f}%",
        ])

        append_trade_plan(
            lines,
            item["watch_trade"]
        )

        lines.extend([
            f"شرط فعال‌سازی LONG: {item['activation']}",
            "وضعیت: فقط تحت‌نظر؛ تا تحقق شرط ورود نکن.",
            "",
            *indicator_lines(item["data"])
        ])

    # ======================================
    # SHORT WATCHLIST
    # ======================================

    lines.extend([
        "",
        "================================",
        "🔴 فرصت‌های تحت‌نظر SHORT",
        "================================",
        "این موارد سیگنال قطعی نیستند."
    ])

    if not watch_shorts:
        lines.append(
            "فرصت SHORT با امتیاز کافی پیدا نشد."
        )

    for rank, item in enumerate(watch_shorts, 1):

        lines.extend([
            "",
            f"#{rank} | {item['symbol']}",
            f"امتیاز SHORT: {item['short_score']}/100",
            f"امتیاز LONG: {item['long_score']}/100",
            f"قیمت LBank: {fmt(item['lbank_price'])}",
            f"اختلاف قیمت: {item['difference']:.3f}%",
        ])

        append_trade_plan(
            lines,
            item["watch_trade"]
        )

        lines.extend([
            f"شرط فعال‌سازی SHORT: {item['activation']}",
            "وضعیت: فقط تحت‌نظر؛ تا تحقق شرط ورود نکن.",
            "",
            *indicator_lines(item["data"])
        ])

    # ======================================
    # OTHER SYMBOLS
    # ======================================

    lines.extend([
        "",
        "================================",
        "📋 سایر نمادهای تحلیل‌شده",
        "================================"
    ])

    for item in results:

        if item["side"]:
            continue

        if item["watch_score"] >= MIN_WATCH_SCORE:
            continue

        lines.append(
            f"⚪ {item['symbol']} | "
            f"LONG {item['long_score']} | "
            f"SHORT {item['short_score']} | "
            f"بدون ورود"
        )

        if item["reasons"]:
            lines.append(
                "دلیل: "
                + "؛ ".join(item["reasons"][:3])
            )

    # ======================================
    # SKIPPED AND ERRORS
    # ======================================

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

    # ======================================
    # DISCLAIMERS
    # ======================================

    lines.extend([

        "",
        "================================",
        "⚠️ توضیحات و مدیریت ریسک",
        "================================",

        "امتیاز واچ‌لیست، احتمال موفقیت یا تضمین سود نیست.",

        "پلن Entry/SL/TP در واچ‌لیست مشروط است و به معنی ورود نیست.",

        "شرط فعال‌سازی باید در کندل بسته‌شده بررسی شود.",

        "کندل‌ها و اندیکاتورها از OKX Futures دریافت شده‌اند.",

        "قیمت مرجع پلن از آخرین قیمت ثبت‌شده LBank گرفته شده است.",

        "اختلاف قیمت و نقدشوندگی را پیش از معامله دوباره بررسی کن.",

        "دفتر سفارش و Funding Rate در این نسخه بررسی نمی‌شوند.",

        "ربات هیچ سفارشی ثبت نمی‌کند.",

        "حد ضرر تضمین اجرای دقیق در قیمت تعیین‌شده نیست.",

        "اهرم بالا ریسک لیکوییدشدن را به‌شدت افزایش می‌دهد.",

        "در صورت نبود تأیید معتبر: ورود نکن."
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
                    f"{result['side'] or 'NO SIGNAL'} | "
                    f"LONG {result['long_score']} | "
                    f"SHORT {result['short_score']}"
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