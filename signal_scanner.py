#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OKX_URL = "https://www.okx.com/api/v5/market/candles"

CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT",
]

MAX_SYMBOLS = 10
MIN_TURNOVER = 100000

MAX_SNAPSHOT_AGE_MINUTES = 20
PRICE_DIFF_LIMIT = 0.75

MAX_RETRIES = 3
MAX_WORKERS = 5

MIN_VOLUME_RATIO = 1.2
MIN_15M_VOLUME_RATIO = 0.8

MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0

MIN_WATCH_SCORE = 55
MIN_SIGNAL_SCORE = 82

MAX_LEVERAGE = 2
RISK_PER_TRADE_PERCENT = 0.5

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
}

BLOCKED_SYMBOLS = {
    "SOXSUSDT", "SOXLUSDT", "QQQUSDT",
    "TQQQUSDT", "SKHYNIXUSDT", "SKHYUSDT",
    "SNXXUSDT", "MUSTOCKUSDT",
}

HEADERS = {
    "User-Agent": "LBank-Futures-Signal-Bot/12.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class SkipSymbol(Exception):
    pass


# ============================================================
# HELPERS
# ============================================================

def number(value, default=None):
    """Safely convert values to finite floats."""
    try:
        if value is None:
            return default

        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def fmt(value):
    value = number(value)

    if value is None:
        return "N/A"

    return f"{value:,.8g}"


def safe_div(a, b):
    a = number(a)
    b = number(b)

    if a is None or b is None or b == 0:
        return None

    return a / b


def get_first(row, keys):
    if not isinstance(row, dict):
        return None

    for key in keys:
        if row.get(key) is not None:
            return row[key]

    return None


def parse_timestamp(value):
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            x = float(value)
            return x / 1000 if x > 1e12 else x

        text = str(value).strip()

        if text.isdigit():
            return parse_timestamp(int(text))

        dt = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()

    except Exception:
        return None


def safe_float_text(value):
    value = number(value)

    if value is None:
        return "N/A"

    return f"{value:.1f}"


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def ema_series(values, period):
    out = [None] * len(values)

    if len(values) < period:
        return out

    current = sum(values[:period]) / period
    out[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * alpha
            + current * (1 - alpha)
        )

        out[i] = current

    return out


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
        return 50 if avg_gain == 0 else 100

    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(values):
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)

    line = [
        f - s
        for f, s in zip(fast, slow)
        if f is not None and s is not None
    ]

    if len(line) < 9:
        return None, None, None

    signal_series = ema_series(line, 9)
    signal = signal_series[-1]

    if signal is None:
        return None, None, None

    return line[-1], signal, line[-1] - signal


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        previous_close = candles[i - 1][4]

        trs.append(max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        ))

    return sum(trs[-period:]) / period


# ============================================================
# LOAD LBANK SNAPSHOTS
# ============================================================

def load_latest_lbank():
    if not DATA_FILE.exists():
        raise RuntimeError(
            f"Data file not found: {DATA_FILE}"
        )

    latest = {}

    with DATA_FILE.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):

            if not line.strip():
                continue

            try:
                row = json.loads(line)

                symbol = str(
                    get_first(
                        row,
                        ["symbol", "instId", "instrument"]
                    ) or ""
                ).strip().upper()

                if symbol not in CORE_SYMBOLS:
                    continue

                price = number(get_first(
                    row,
                    [
                        "lastPrice",
                        "price",
                        "last",
                        "close",
                        "last_price"
                    ]
                ))

                turnover = number(get_first(
                    row,
                    [
                        "turnover24h",
                        "turnover",
                        "quoteVolume",
                        "quote_volume",
                        "volValue",
                        "amount",
                        "volume_usdt"
                    ]
                ))

                timestamp = parse_timestamp(get_first(
                    row,
                    [
                        "timestamp",
                        "ts",
                        "time",
                        "updated_at",
                        "updateTime"
                    ]
                ))

                if (
                    not symbol
                    or price is None
                    or price <= 0
                    or timestamp is None
                ):
                    continue

                item = {
                    "symbol": symbol,
                    "price": price,
                    "turnover": turnover or 0,
                    "timestamp": timestamp,
                }

                if (
                    symbol not in latest
                    or timestamp >= latest[symbol]["timestamp"]
                ):
                    latest[symbol] = item

            except Exception as exc:
                print(
                    f"Invalid JSONL line {line_no}: {exc}"
                )

    now = time.time()
    valid = {}

    for symbol, item in latest.items():

        age = (now - item["timestamp"]) / 60

        if (
            -5 <= age <= MAX_SNAPSHOT_AGE_MINUTES
            and item["turnover"] >= MIN_TURNOVER
            and symbol not in BLOCKED_SYMBOLS
        ):
            valid[symbol] = item

    print(
        f"Fresh core snapshots: "
        f"{len(valid)}/{len(CORE_SYMBOLS)}"
    )

    missing = [
        symbol for symbol in CORE_SYMBOLS
        if symbol not in valid
    ]

    if missing:
        print(
            "Missing/stale/low-turnover: "
            + ", ".join(missing)
        )

    return valid


# ============================================================
# OKX CANDLE DATA
# ============================================================

def convert_symbol(symbol):
    if not symbol.endswith("USDT"):
        raise SkipSymbol(
            f"Unsupported symbol: {symbol}"
        )

    return f"{symbol[:-4]}-USDT-SWAP"


def get_candles(symbol, timeframe):

    params = {
        "instId": convert_symbol(symbol),
        "bar": INTERVALS[timeframe],
        "limit": "250",
    }

    last_error = None

    for attempt in range(MAX_RETRIES):

        try:
            response = SESSION.get(
                OKX_URL,
                params=params,
                timeout=15
            )

            response.raise_for_status()

            payload = response.json()

            if payload.get("code") != "0":
                raise SkipSymbol(
                    payload.get("msg", "OKX API error")
                )

            candles = []

            for row in reversed(payload.get("data", [])):

                if len(row) < 9:
                    continue

                # Only completed candles.
                if str(row[8]) != "1":
                    continue

                candle = [
                    number(row[0]),
                    number(row[1]),
                    number(row[2]),
                    number(row[3]),
                    number(row[4]),
                    number(row[5]),
                ]

                if all(x is not None for x in candle):
                    candles.append(candle)

            if len(candles) < 205:
                raise SkipSymbol(
                    f"Not enough completed {timeframe} candles: "
                    f"{len(candles)}"
                )

            return candles

        except SkipSymbol:
            raise

        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)

    raise SkipSymbol(
        f"OKX data unavailable: {last_error}"
    )


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def timeframe_analysis(candles):

    closes = [c[4] for c in candles]
    volumes = [c[5] for c in candles]

    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)

    current = closes[-1]

    rsi_value = rsi(closes)

    macd_value, macd_signal, macd_hist = macd(closes)

    avg_volume = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21
        else None
    )

    volume_ratio = (
        safe_div(volumes[-1], avg_volume)
        if avg_volume
        else None
    )

    current_atr = atr(candles)

    bullish = (
        ema20[-1] is not None
        and ema50[-1] is not None
        and ema200[-1] is not None
        and current > ema20[-1]
        and ema20[-1] > ema50[-1]
        and ema50[-1] > ema200[-1]
    )

    bearish = (
        ema20[-1] is not None
        and ema50[-1] is not None
        and ema200[-1] is not None
        and current < ema20[-1]
        and ema20[-1] < ema50[-1]
        and ema50[-1] < ema200[-1]
    )

    recent = candles[-50:]

    support = min(c[3] for c in recent)
    resistance = max(c[2] for c in recent)

    return {
        "price": current,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "volume_ratio": volume_ratio,
        "atr": current_atr,
        "bullish": bullish,
        "bearish": bearish,
        "support": support,
        "resistance": resistance,
    }


# ============================================================
# DIRECTION SCORING
# ============================================================

def score_direction(data, direction):

    score = 0
    reasons = []

    h4 = data["4h"]
    h1 = data["1h"]
    m15 = data["15m"]
    m5 = data["5m"]

    is_long = direction == "LONG"

    def trend_ok(item):
        return (
            item["bullish"]
            if is_long
            else item["bearish"]
        )

    def macd_ok(item):

        value = item["macd_hist"]

        if value is None:
            return False

        return value > 0 if is_long else value < 0

    # Higher timeframe trend.

    if trend_ok(h4):
        score += 25
        reasons.append("روند 4H هم‌جهت")
    else:
        score -= 20
        reasons.append("روند 4H مخالف")

    if trend_ok(h1):
        score += 20
        reasons.append("روند 1H هم‌جهت")
    else:
        score -= 15
        reasons.append("روند 1H مخالف")

    # MACD confirmation.

    for item, points in [
        (h4, 10),
        (h1, 10),
        (m15, 8),
        (m5, 7),
    ]:

        if macd_ok(item):
            score += points
        else:
            score -= points * 0.6

    # RSI.

    rsi4 = h4["rsi"]
    rsi1 = h1["rsi"]
    rsi15 = m15["rsi"]

    if is_long:

        if rsi4 is not None:

            if rsi4 > 75:
                score -= 15
                reasons.append("اشباع خرید 4H")

            elif 50 <= rsi4 <= 70:
                score += 8

        if rsi1 is not None:

            if rsi1 > 75:
                score -= 10

            elif 50 <= rsi1 <= 70:
                score += 5

        if rsi15 is not None and rsi15 < 40:
            score -= 5

    else:

        if rsi4 is not None:

            if rsi4 < 25:
                score -= 15
                reasons.append("اشباع فروش 4H")

            elif 30 <= rsi4 <= 50:
                score += 8

        if rsi1 is not None:

            if rsi1 < 25:
                score -= 10

            elif 30 <= rsi1 <= 50:
                score += 5

        if rsi15 is not None and rsi15 > 60:
            score -= 5

    # Volume confirmation.

    volume5 = m5["volume_ratio"]
    volume15 = m15["volume_ratio"]

    if (
        volume5 is not None
        and volume5 >= MIN_VOLUME_RATIO
    ):
        score += 10
        reasons.append("حجم 5m مناسب")
    else:
        score -= 12
        reasons.append("حجم 5m ضعیف")

    if (
        volume15 is not None
        and volume15 >= MIN_15M_VOLUME_RATIO
    ):
        score += 5
    else:
        score -= 5

    return max(0, min(100, score)), reasons


# ============================================================
# TRADE PLAN
# ============================================================

def make_trade(symbol, price, data, direction, score):

    atr_value = data["1h"]["atr"]

    if atr_value is None or price <= 0:
        return None

    risk_distance = atr_value * 1.5

    risk_percent = (
        risk_distance / price
    ) * 100

    if not (
        MIN_RISK_PERCENT
        <= risk_percent
        <= MAX_RISK_PERCENT
    ):
        return None

    if direction == "LONG":

        stop = price - risk_distance
        risk = price - stop

        tp1 = price + risk
        tp2 = price + risk * 2
        tp3 = price + risk * 3

    else:

        stop = price + risk_distance
        risk = stop - price

        tp1 = price - risk
        tp2 = price - risk * 2
        tp3 = price - risk * 3

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": price,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr1": 1.0,
        "rr2": 2.0,
        "rr3": 3.0,
        "risk_percent": risk_percent,
        "leverage": MAX_LEVERAGE,
        "margin_mode": "ISOLATED",
    }


# ============================================================
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(item):

    symbol = item["symbol"]
    lbank_price = item["price"]

    result = {
        "symbol": symbol,
        "status": "REJECTED",
        "reason": "",
        "price": lbank_price,
    }

    try:

        candles = {}

        for timeframe in INTERVALS:

            candles[timeframe] = get_candles(
                symbol,
                timeframe
            )

        data = {
            tf: timeframe_analysis(candles[tf])
            for tf in INTERVALS
        }

        okx_price = data["5m"]["price"]

        price_difference = (
            abs(lbank_price - okx_price)
            / okx_price
        ) * 100

        if price_difference > PRICE_DIFF_LIMIT:

            result["reason"] = (
                f"اختلاف قیمت LBank/OKX برابر "
                f"{price_difference:.2f}% است"
            )

            return result

        long_score, long_reasons = score_direction(
            data,
            "LONG"
        )

        short_score, short_reasons = score_direction(
            data,
            "SHORT"
        )

        if long_score >= short_score:

            direction = "LONG"
            score = long_score
            reasons = long_reasons

        else:

            direction = "SHORT"
            score = short_score
            reasons = short_reasons

        result.update({
            "direction": direction,
            "score": score,
            "data": data,
            "okx_price": okx_price,
            "price_difference": price_difference,
            "reasons": reasons,
        })

        h4 = data["4h"]
        h1 = data["1h"]
        m15 = data["15m"]
        m5 = data["5m"]

        if direction == "LONG":

            trend_confirmed = (
                h4["bullish"]
                and h1["bullish"]
            )

            macd_confirmed = (
                h4["macd_hist"] is not None
                and h1["macd_hist"] is not None
                and h4["macd_hist"] > 0
                and h1["macd_hist"] > 0
            )

            rsi_confirmed = (
                h1["rsi"] is not None
                and 50 <= h1["rsi"] <= 75
            )

        else:

            trend_confirmed = (
                h4["bearish"]
                and h1["bearish"]
            )

            macd_confirmed = (
                h4["macd_hist"] is not None
                and h1["macd_hist"] is not None
                and h4["macd_hist"] < 0
                and h1["macd_hist"] < 0
            )

            rsi_confirmed = (
                h1["rsi"] is not None
                and 25 <= h1["rsi"] <= 50
            )

        volume_confirmed = (
            m5["volume_ratio"] is not None
            and m5["volume_ratio"] >= MIN_VOLUME_RATIO
            and m15["volume_ratio"] is not None
            and m15["volume_ratio"] >= MIN_15M_VOLUME_RATIO
        )

        all_confirmed = (
            trend_confirmed
            and macd_confirmed
            and rsi_confirmed
            and volume_confirmed
        )

        if score >= MIN_SIGNAL_SCORE and all_confirmed:

            trade = make_trade(
                symbol,
                lbank_price,
                data,
                direction,
                score
            )

            if trade:

                result["status"] = "SIGNAL"
                result["trade"] = trade

            else:

                result["status"] = "WATCH"
                result["reason"] = (
                    "فاصله حد ضرر خارج از محدوده مجاز"
                )

        elif score >= MIN_WATCH_SCORE:

            result["status"] = "WATCH"
            result["reason"] = (
                "تأییدیه‌های ورود کامل نیست"
            )

        else:

            result["status"] = "REJECTED"
            result["reason"] = (
                "امتیاز تکنیکال کافی نیست"
            )

        return result

    except SkipSymbol as exc:

        result["status"] = "REJECTED"
        result["reason"] = str(exc)

        return result

    except Exception as exc:

        result["status"] = "ERROR"
        result["reason"] = str(exc)

        return result


# ============================================================
# REPORT FORMATTING
# ============================================================

def format_trade(trade):

    if not isinstance(trade, dict):
        return "Trade details unavailable."

    direction = trade.get("direction", "UNKNOWN")

    entry = number(trade.get("entry"))
    stop = number(trade.get("stop"))

    tp1 = number(trade.get("tp1"))
    tp2 = number(trade.get("tp2"))
    tp3 = number(trade.get("tp3"))

    risk_percent = number(
        trade.get("risk_percent"),
        0.0
    )

    leverage = int(
        number(trade.get("leverage"), 1)
    )

    return "\n".join([
        f"Direction: {direction}",
        f"Entry: {fmt(entry)}",
        f"Stop Loss: {fmt(stop)}",
        f"TP1: {fmt(tp1)} | RR: 1R",
        f"TP2: {fmt(tp2)} | RR: 2R",
        f"TP3: {fmt(tp3)} | RR: 3R",
        f"Stop distance: {risk_percent:.2f}%",
        f"Leverage: {leverage}x ISOLATED",
        f"Maximum account risk: {RISK_PER_TRADE_PERCENT:.2f}%"
    ])


def format_market_status(item):

    symbol = item.get("symbol", "UNKNOWN")

    status = str(
        item.get("status", "UNKNOWN")
    ).upper()

    direction = str(
        item.get("direction", "-")
    ).upper()

    score = number(
        item.get("score"),
        0.0
    )

    price = number(item.get("price"))

    lines = [
        f"• {symbol} | {status}",
        f"Price: {fmt(price)} | Score: {score:.1f}/100"
    ]

    reason = item.get("reason")

    if reason:
        lines.append(f"Reason: {reason}")

    data = item.get("data") or {}

    if not isinstance(data, dict) or not data:
        lines.append("NO ENTRY.")
        return "\n".join(lines)

    def get_tf(tf):
        value = data.get(tf, {})
        return value if isinstance(value, dict) else {}

    def trend(tf):

        value = get_tf(tf)

        if value.get("bullish"):
            return "BULLISH"

        if value.get("bearish"):
            return "BEARISH"

        return "NEUTRAL"

    def macd_state(tf):

        histogram = number(
            get_tf(tf).get("macd_hist")
        )

        if histogram is None:
            return "N/A"

        if histogram > 0:
            return "POSITIVE"

        if histogram < 0:
            return "NEGATIVE"

        return "NEUTRAL"

    def volume_text(tf):

        value = number(
            get_tf(tf).get("volume_ratio")
        )

        if value is None:
            return "N/A"

        return f"{value:.2f}x"

    def rsi_text(tf):

        value = number(
            get_tf(tf).get("rsi")
        )

        if value is None:
            return "N/A"

        return f"{value:.1f}"

    lines.extend([
        f"Direction: {direction}",
        f"Trend 4H / 1H: {trend('4h')} / {trend('1h')}",
        f"RSI 4H / 1H: {rsi_text('4h')} / {rsi_text('1h')}",
        (
            "MACD 4H / 1H / 15M / 5M: "
            f"{macd_state('4h')} / {macd_state('1h')} / "
            f"{macd_state('15m')} / {macd_state('5m')}"
        ),
        (
            "Volume 15M / 5M: "
            f"{volume_text('15m')} / {volume_text('5m')}"
        )
    ])

    one_hour = get_tf("1h")

    support = number(one_hour.get("support"))
    resistance = number(one_hour.get("resistance"))

    lines.append(
        f"1H Support: {fmt(support)} | "
        f"Resistance: {fmt(resistance)}"
    )

    trade = item.get("trade")

    if status == "SIGNAL" and isinstance(trade, dict):

        lines.append(format_trade(trade))

    elif status == "WATCH":

        lines.append("WATCH ONLY — NO ENTRY.")

    else:

        lines.append("NO ENTRY.")

    return "\n".join(lines)


# ============================================================
# COMPLETE REPORT
# ============================================================

def build_report(results, total_symbols, missing_symbols):

    results = results or []
    missing_symbols = missing_symbols or []

    result_map = {}

    for item in results:

        if isinstance(item, dict):

            symbol = item.get("symbol")

            if symbol:
                result_map[symbol] = item

    signal_items = [
        item for item in results
        if isinstance(item, dict)
        and str(item.get("status", "")).upper() == "SIGNAL"
    ]

    watch_items = [
        item for item in results
        if isinstance(item, dict)
        and str(item.get("status", "")).upper() == "WATCH"
    ]

    lines = [
        "LBANK AI FUTURES SCANNER",
        "MULTI-TIMEFRAME: 5M / 15M / 1H / 4H",
        "=" * 35,
        f"Core symbols: {len(CORE_SYMBOLS)}",
        f"Valid snapshots: {total_symbols}",
        f"Confirmed signals: {len(signal_items)}",
        f"Watch candidates: {len(watch_items)}",
        "",
        "MARKET STATUS — ALL 10 SYMBOLS",
        "-" * 35
    ]

    for symbol in CORE_SYMBOLS:

        item = result_map.get(symbol)

        if item:

            lines.append(
                format_market_status(item)
            )

        else:

            lines.append(
                f"• {symbol} | NO DATA / NO ENTRY\n"
                "Reason: No valid fresh LBank snapshot."
            )

        lines.append("")

    lines.extend([
        "=" * 35,
        "CONFIRMED SIGNALS",
        "-" * 35
    ])

    if signal_items:

        for item in signal_items:

            lines.append(
                format_market_status(item)
            )

            lines.append("")

    else:

        lines.append(
            "NO CONFIRMED SIGNAL — NO ENTRY."
        )

        lines.append("")

    lines.extend([
        "WATCHLIST",
        "-" * 35
    ])

    if watch_items:

        for item in watch_items:

            symbol = item.get("symbol", "UNKNOWN")
            direction = item.get("direction", "-")

            score = number(
                item.get("score"),
                0.0
            )

            reason = item.get(
                "reason",
                "Conditions not fully confirmed."
            )

            lines.append(
                f"{symbol}: {direction} | Score {score:.1f}/100\n"
                f"Reason: {reason}\n"
                "WATCH ONLY — DO NOT ENTER."
            )

    else:

        lines.append("No watch candidates.")

    lines.extend([
        "",
        "=" * 35,
        "RISK MANAGEMENT",
        "-" * 35,
        "Maximum leverage: 2x.",
        "Margin mode: ISOLATED.",
        (
            "Maximum planned account risk per trade: "
            f"{RISK_PER_TRADE_PERCENT:.2f}%."
        ),
        "Account risk is NOT the same as leverage.",
        "Position size must be calculated from stop distance.",
        "No confirmed signal means NO ENTRY.",
        "High leverage increases liquidation risk.",
        "",
        "Technical analysis is not a guarantee of profit."
    ])

    if missing_symbols:

        lines.extend([
            "",
            "MISSING / STALE SNAPSHOTS",
            "-" * 35,
            ", ".join(missing_symbols)
        ])

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:

        print(
            "Telegram credentials missing. "
            "Report was not sent."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    max_length = 3900

    chunks = []
    remaining = str(message)

    while remaining:

        if len(remaining) <= max_length:

            chunks.append(remaining)
            break

        split_at = remaining.rfind(
            "\n",
            0,
            max_length
        )

        if split_at < max_length // 2:
            split_at = max_length

        chunks.append(
            remaining[:split_at]
        )

        remaining = remaining[split_at:].lstrip()

    success = True

    for index, chunk in enumerate(chunks, start=1):

        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True
        }

        try:

            response = SESSION.post(
                url,
                json=payload,
                timeout=20
            )

            if response.status_code != 200:

                success = False

                print(
                    f"Telegram error {index}: "
                    f"{response.status_code} "
                    f"{response.text[:300]}"
                )

            else:

                result = response.json()

                if not result.get("ok", False):

                    success = False

                    print(
                        f"Telegram rejected message {index}: "
                        f"{result}"
                    )

        except Exception as exc:

            success = False

            print(
                f"Telegram send error: {exc}"
            )

        time.sleep(0.5)

    return success


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("LBank AI Futures Scanner starting...")
    print("=" * 60)

    snapshots = load_latest_lbank()

    if not isinstance(snapshots, dict):
        snapshots = {}

    missing_symbols = [
        symbol
        for symbol in CORE_SYMBOLS
        if symbol not in snapshots
    ]

    results = []

    print(
        f"Valid snapshots: "
        f"{len(snapshots)} / {len(CORE_SYMBOLS)}"
    )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                item
            ): symbol

            for symbol, item in snapshots.items()
        }

        for future in as_completed(futures):

            symbol = futures[future]

            try:

                result = future.result()

                if not isinstance(result, dict):

                    result = {
                        "symbol": symbol,
                        "status": "ERROR",
                        "reason": "Invalid analyzer result."
                    }

                result.setdefault(
                    "symbol",
                    symbol
                )

                results.append(result)

                print(
                    f"{symbol}: "
                    f"{result.get('status', 'UNKNOWN')} | "
                    f"Score: {result.get('score', 0)}"
                )

            except Exception as exc:

                print(
                    f"{symbol} analysis error: {exc}"
                )

                results.append({
                    "symbol": symbol,
                    "status": "ERROR",
                    "reason": str(exc)
                })

    report = build_report(
        results=results,
        total_symbols=len(snapshots),
        missing_symbols=missing_symbols
    )

    REPORT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print("\n" + report)

    sent = send_telegram(report)

    if sent:

        print(
            "\nTelegram report sent successfully."
        )

    else:

        print(
            "\nTelegram report could not be fully delivered."
        )

    print("\nScanner finished.")


if __name__ == "__main__":
    main()