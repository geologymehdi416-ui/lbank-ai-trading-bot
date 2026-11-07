#!/usr/bin/env python3
import os
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
MIN_15M_VOLUME_RATIO = 0.8

MIN_RISK_PERCENT = 0.15
MAX_RISK_PERCENT = 3.0

MIN_WATCH_SCORE = 55
MIN_SIGNAL_SCORE = 78

MAX_WATCH_PER_SIDE = 5

INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
}

BLOCKED_SYMBOLS = {
    "SOXSUSDT",
    "SOXLUSDT",
    "QQQUSDT",
    "TQQQUSDT",
    "SKHYNIXUSDT",
    "SKHYUSDT",
    "SNXXUSDT",
    "MUSTOCKUSDT",
}

HEADERS = {
    "User-Agent": "LBank-Futures-Signal-Bot/9.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


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


def safe_div(a, b):
    if b is None or b == 0:
        return 0
    return a / b


def parse_timestamp(value):
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            value = float(value)

            if value > 1e12:
                value /= 1000

            return value

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


def get_first(row, keys):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


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

    signal_values = ema_series(line, 9)

    macd_value = line[-1]
    signal_value = signal_values[-1]

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
        raise RuntimeError(
            f"LBank data file not found: {DATA_FILE}"
        )

    latest = {}

    with DATA_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        for line_number, line in enumerate(file, 1):

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

                price = number(
                    get_first(
                        row,
                        [
                            "price",
                            "last",
                            "lastPrice",
                            "close",
                            "last_price"
                        ]
                    )
                )

                turnover = number(
                    get_first(
                        row,
                        [
                            "turnover",
                            "quoteVolume",
                            "quote_volume",
                            "volValue",
                            "amount",
                            "volume_usdt"
                        ]
                    )
                )

                timestamp = parse_timestamp(
                    get_first(
                        row,
                        [
                            "timestamp",
                            "ts",
                            "time",
                            "updated_at",
                            "updateTime"
                        ]
                    )
                )

                if not symbol or price is None or price <= 0:
                    continue

                if timestamp is None:
                    timestamp = time.time()

                if symbol not in latest:
                    latest[symbol] = {
                        "symbol": symbol,
                        "price": price,
                        "turnover": turnover or 0,
                        "timestamp": timestamp,
                    }

                elif timestamp >= latest[symbol]["timestamp"]:
                    latest[symbol] = {
                        "symbol": symbol,
                        "price": price,
                        "turnover": turnover or 0,
                        "timestamp": timestamp,
                    }

            except Exception as exc:
                print(
                    f"Invalid JSONL line {line_number}: {exc}"
                )

    now = time.time()

    valid = []

    for item in latest.values():

        age = (now - item["timestamp"]) / 60

        if age > MAX_SNAPSHOT_AGE_MINUTES:
            continue

        if item["turnover"] < MIN_TURNOVER:
            continue

        if item["symbol"] in BLOCKED_SYMBOLS:
            continue

        valid.append(item)

    valid.sort(
        key=lambda x: x["turnover"],
        reverse=True
    )

    return valid[:MAX_SYMBOLS]


# ==========================================
# OKX DATA
# ==========================================

def convert_symbol(symbol):

    symbol = symbol.upper().replace("/", "")

    if symbol.endswith("-USDT-SWAP"):
        return symbol

    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"

    raise SkipSymbol(
        f"Unsupported symbol format: {symbol}"
    )


def get_candles(symbol, timeframe):

    inst_id = convert_symbol(symbol)

    params = {
        "instId": inst_id,
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

            result = response.json()

            if result.get("code") != "0":
                raise RuntimeError(
                    result.get("msg", "OKX API error")
                )

            raw = result.get("data", [])

            candles = []

            for item in reversed(raw):

                if len(item) < 9:
                    continue

                if str(item[8]) != "1":
                    continue

                candle = [
                    number(item[0]),
                    number(item[1]),
                    number(item[2]),
                    number(item[3]),
                    number(item[4]),
                    number(item[5]),
                ]

                if any(x is None for x in candle):
                    continue

                candles.append(candle)

            if len(candles) < 205:
                raise SkipSymbol(
                    f"Not enough closed candles: {timeframe}"
                )

            return candles

        except SkipSymbol:
            raise

        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)

    raise RuntimeError(
        f"OKX candles failed: {last_error}"
    )


# ==========================================
# TIMEFRAME ANALYSIS
# ==========================================

def timeframe_analysis(candles):

    closes = [c[4] for c in candles]
    volumes = [c[5] for c in candles]

    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)

    current = closes[-1]

    current_ema20 = ema20[-1]
    current_ema50 = ema50[-1]
    current_ema200 = ema200[-1]

    previous_ema20 = ema20[-2]

    rsi_value = rsi(closes)

    macd_value, signal_value, histogram = macd(
        closes
    )

    average_volume = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21
        else None
    )

    volume_ratio = (
        safe_div(volumes[-1], average_volume)
        if average_volume
        else None
    )

    current_atr = atr(candles)

    bullish = (
        current > current_ema20
        and current_ema20 > current_ema50
        and current_ema50 > current_ema200
    )

    bearish = (
        current < current_ema20
        and current_ema20 < current_ema50
        and current_ema50 < current_ema200
    )

    return {
        "price": current,
        "ema20": current_ema20,
        "ema50": current_ema50,
        "ema200": current_ema200,
        "previous_ema20": previous_ema20,
        "rsi": rsi_value,
        "macd": macd_value,
        "macd_signal": signal_value,
        "macd_hist": histogram,
        "volume_ratio": volume_ratio,
        "atr": current_atr,
        "bullish": bullish,
        "bearish": bearish,
    }


# ==========================================
# SIGNAL SCORING
# ==========================================

def score_direction(data, direction):

    score = 0
    reasons = []

    h4 = data["4h"]
    h1 = data["1h"]
    m15 = data["15m"]
    m5 = data["5m"]

    is_long = direction == "LONG"

    def correct_trend(item):
        return (
            item["bullish"]
            if is_long
            else item["bearish"]
        )

    def correct_macd(item):
        value = item["macd_hist"]

        if value is None:
            return False

        return value > 0 if is_long else value < 0

    # Higher timeframe
    if correct_trend(h4):
        score += 25
        reasons.append("روند 4H هم‌جهت")

    else:
        score -= 20
        reasons.append("روند 4H مخالف")

    if correct_trend(h1):
        score += 20
        reasons.append("روند 1H هم‌جهت")

    else:
        score -= 15
        reasons.append("روند 1H مخالف")

    # MACD
    if correct_macd(h4):
        score += 10
        reasons.append("MACD 4H تأیید")

    else:
        score -= 8

    if correct_macd(h1):
        score += 10
        reasons.append("MACD 1H تأیید")

    else:
        score -= 8

    if correct_macd(m15):
        score += 8

    else:
        score -= 5

    if correct_macd(m5):
        score += 7

    else:
        score -= 4

    # RSI
    rsi4 = h4["rsi"]
    rsi1 = h1["rsi"]
    rsi15 = m15["rsi"]

    if is_long:

        if rsi4 is not None and rsi4 > 75:
            score -= 15
            reasons.append("اشباع خرید 4H")

        elif rsi4 is not None and 50 <= rsi4 <= 70:
            score += 8

        if rsi1 is not None and rsi1 > 75:
            score -= 10

        elif rsi1 is not None and 50 <= rsi1 <= 70:
            score += 5

        if rsi15 is not None and rsi15 < 40:
            score -= 5

    else:

        if rsi4 is not None and rsi4 < 25:
            score -= 15
            reasons.append("اشباع فروش 4H")

        elif rsi4 is not None and 30 <= rsi4 <= 50:
            score += 8

        if rsi1 is not None and rsi1 < 25:
            score -= 10

        elif rsi1 is not None and 30 <= rsi1 <= 50:
            score += 5

        if rsi15 is not None and rsi15 > 60:
            score -= 5

    # Volume
    volume5 = m5["volume_ratio"]
    volume15 = m15["volume_ratio"]

    if volume5 is not None and volume5 >= MIN_VOLUME_RATIO:
        score += 10
        reasons.append("حجم 5m مناسب")

    else:
        score -= 12
        reasons.append("حجم 5m ضعیف")

    if volume15 is not None and volume15 >= MIN_15M_VOLUME_RATIO:
        score += 5

    else:
        score -= 5

    return clamp(score, 0, 100), reasons


# ==========================================
# TRADE PLAN
# ==========================================

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

        targets = [
            price + risk * 1.0,
            price + risk * 2.0,
            price + risk * 3.0,
        ]

    else:

        stop = price + risk_distance

        risk = stop - price

        targets = [
            price - risk * 1.0,
            price - risk * 2.0,
            price - risk * 3.0,
        ]

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": price,
        "stop": stop,
        "targets": targets,
        "risk_percent": risk_percent,
        "reward_risk": 1.0,
    }


# ==========================================
# SYMBOL ANALYSIS
# ==========================================

def analyze_symbol(item):

    symbol = item["symbol"]
    lbank_price = item["price"]

    result = {
        "symbol": symbol,
        "status": "REJECTED",
        "reason": "",
    }

    try:

        candles = {}

        for timeframe in INTERVALS:
            candles[timeframe] = get_candles(
                symbol,
                timeframe
            )

        data = {
            timeframe: timeframe_analysis(
                candles[timeframe]
            )
            for timeframe in INTERVALS
        }

        okx_price = data["5m"]["price"]

        price_difference = (
            abs(lbank_price - okx_price)
            / okx_price
        ) * 100

        if price_difference > PRICE_DIFF_LIMIT:

            result["reason"] = (
                f"اختلاف قیمت LBank/OKX: "
                f"{price_difference:.2f}%"
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
            "score": score,
            "direction": direction,
            "price": lbank_price,
            "okx_price": okx_price,
            "price_difference": price_difference,
            "data": data,
            "reasons": reasons,
        })

        # شروط سخت‌گیرانه سیگنال قطعی
        h4 = data["4h"]
        h1 = data["1h"]
        m15 = data["15m"]
        m5 = data["5m"]

        if direction == "LONG":

            trend_ok = (
                h4["bullish"]
                and h1["bullish"]
            )

            macd_ok = (
                h4["macd_hist"] > 0
                and h1["macd_hist"] > 0
            )

        else:

            trend_ok = (
                h4["bearish"]
                and h1["bearish"]
            )

            macd_ok = (
                h4["macd_hist"] < 0
                and h1["macd_hist"] < 0
            )

        volume_ok = (
            m5["volume_ratio"] is not None
            and m5["volume_ratio"] >= MIN_VOLUME_RATIO
            and m15["volume_ratio"] is not None
            and m15["volume_ratio"] >= MIN_15M_VOLUME_RATIO
        )

        if (
            score >= MIN_SIGNAL_SCORE
            and trend_ok
            and macd_ok
            and volume_ok
        ):

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
                    "ریسک معامله خارج از محدوده مجاز است"
                )

        elif score >= MIN_WATCH_SCORE:

            result["status"] = "WATCH"

            result["reason"] = (
                "شرایط سیگنال قطعی کامل نیست"
            )

        else:

            result["reason"] = (
                "امتیاز و تأییدیه‌ها کافی نیست"
            )

        return result

    except SkipSymbol as exc:

        result["reason"] = str(exc)
        return result

    except Exception as exc:

        result["status"] = "ERROR"
        result["reason"] = str(exc)

        return result


# ==========================================
# REPORT
# ==========================================

def format_trade(trade):

    targets = trade["targets"]

    return (
        f"\n{trade['symbol']} | {trade['direction']}\n"
        f"Score: {trade['score']}/100\n"
        f"Entry: {fmt(trade['entry'])}\n"
        f"Stop Loss: {fmt(trade['stop'])}\n"
        f"TP1: {fmt(targets[0])}\n"
        f"TP2: {fmt(targets[1])}\n"
        f"TP3: {fmt(targets[2])}\n"
        f"Risk: {trade['risk_percent']:.2f}%\n"
        f"Reward/Risk TP1: 1:1\n"
    )


def build_report(results, total_symbols):

    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    signals = [
        x for x in results
        if x["status"] == "SIGNAL"
    ]

    watches = [
        x for x in results
        if x["status"] == "WATCH"
    ]

    errors = [
        x for x in results
        if x["status"] == "ERROR"
    ]

    rejected = [
        x for x in results
        if x["status"] == "REJECTED"
    ]

    long_watches = [
        x for x in watches
        if x.get("direction") == "LONG"
    ]

    short_watches = [
        x for x in watches
        if x.get("direction") == "SHORT"
    ]

    long_watches.sort(
        key=lambda x: x.get("score", 0),
        reverse=True
    )

    short_watches.sort(
        key=lambda x: x.get("score", 0),
        reverse=True
    )

    lines = []

    lines.append("LBANK AI FUTURES SCANNER")
    lines.append("=" * 45)
    lines.append(f"Time: {now}")
    lines.append(f"Selected symbols: {total_symbols}")
    lines.append(f"Analyzed: {len(results)}")
    lines.append(f"Signals: {len(signals)}")
    lines.append(f"Watchlist: {len(watches)}")
    lines.append(f"Rejected: {len(rejected)}")
    lines.append(f"Errors: {len(errors)}")

    lines.append("")
    lines.append("IMPORTANT:")
    lines.append(
        "This report is analytical and does not execute orders."
    )
    lines.append(
        "Entry uses the latest LBank snapshot price."
    )
    lines.append(
        "Recheck price and candle confirmation before entry."
    )

    lines.append("")
    lines.append("=" * 45)
    lines.append("CONFIRMED SIGNALS")
    lines.append("=" * 45)

    if not signals:
        lines.append("No confirmed signals. No entry.")

    for item in signals:

        lines.append(
            format_trade(item["trade"])
        )

        lines.append(
            "Reasons: " + " | ".join(item["reasons"])
        )

    lines.append("")
    lines.append("=" * 45)
    lines.append("LONG WATCHLIST")
    lines.append("=" * 45)

    if not long_watches:
        lines.append("No LONG watch candidates.")

    for item in long_watches[:MAX_WATCH_PER_SIDE]:

        lines.append(
            f"{item['symbol']} | "
            f"Score: {item['score']} | "
            f"Price: {fmt(item['price'])}"
        )

        lines.append(
            "Reason: " + item.get("reason", "")
        )

    lines.append("")
    lines.append("=" * 45)
    lines.append("SHORT WATCHLIST")
    lines.append("=" * 45)

    if not short_watches:
        lines.append("No SHORT watch candidates.")

    for item in short_watches[:MAX_WATCH_PER_SIDE]:

        lines.append(
            f"{item['symbol']} | "
            f"Score: {item['score']} | "
            f"Price: {fmt(item['price'])}"
        )

        lines.append(
            "Reason: " + item.get("reason", "")
        )

    lines.append("")
    lines.append("=" * 45)
    lines.append("REJECTED SYMBOLS")
    lines.append("=" * 45)

    for item in rejected:
        lines.append(
            f"{item['symbol']}: {item.get('reason', '')}"
        )

    lines.append("")
    lines.append("=" * 45)
    lines.append("ERRORS")
    lines.append("=" * 45)

    for item in errors:
        lines.append(
            f"{item['symbol']}: {item.get('reason', '')}"
        )

    return "\n".join(lines)


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(text):

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram secrets not configured.")
        return False

    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
    )

    # Telegram message limit
    chunks = [
        text[i:i + 3900]
        for i in range(0, len(text), 3900)
    ]

    success = True

    for chunk in chunks:

        try:

            response = SESSION.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                },
                timeout=20
            )

            response.raise_for_status()

            data = response.json()

            if not data.get("ok"):
                success = False

        except Exception as exc:

            print(
                f"Telegram error: {exc}"
            )

            success = False

    return success


# ==========================================
# MAIN
# ==========================================

def main():

    print("=" * 50)
    print("LBANK AI SIGNAL SCANNER")
    print("=" * 50)

    try:

        symbols = load_latest_lbank()

    except Exception as exc:

        print(
            f"Failed to load LBank data: {exc}"
        )

        raise

    print(
        f"Selected symbols: {len(symbols)}"
    )

    if not symbols:
        raise RuntimeError(
            "No valid LBank symbols found."
        )

    results = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                item
            ): item["symbol"]
            for item in symbols
        }

        for future in as_completed(futures):

            symbol = futures[future]

            try:

                result = future.result()

            except Exception as exc:

                result = {
                    "symbol": symbol,
                    "status": "ERROR",
                    "reason": str(exc),
                }

            results.append(result)

            print(
                f"{symbol}: "
                f"{result.get('status')} "
                f"{result.get('score', '')}"
            )

    report = build_report(
        results,
        len(symbols)
    )

    REPORT_FILE.write_text(
        report,
        encoding="utf-8"
    )

    print("")
    print(report)

    telegram_ok = send_telegram(report)

    print("")
    print(
        f"Telegram sent: {telegram_ok}"
    )

    print("Scanner completed.")


if __name__ == "__main__":
    main()