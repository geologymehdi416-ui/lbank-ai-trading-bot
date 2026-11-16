
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

# ================= CONFIG =================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

OURBIT_BASE = "https://contract.ourbit.com"

MAX_LBANK_AGE = 600
MAX_PRICE_DIVERGENCE = 0.002  # 0.20%

MIN_NET_RR = 1.5
TARGET_RR = 1.8
MAX_SYMBOLS = 12
MAX_SIGNALS = 5

FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002

# Ourbit Futures intervals
TIMEFRAMES = {
    "4h": ("Hour4", 4 * 60 * 60 * 1000),
    "1h": ("Min60", 60 * 60 * 1000),
    "15m": ("Min15", 15 * 60 * 1000),
    "5m": ("Min5", 5 * 60 * 1000),
    "1m": ("Min1", 60 * 1000),
}

CANDLE_LIMIT = 260

session = requests.Session()
session.headers.update({
    "User-Agent": "LBank-Ourbit-Futures-Sniper/1.0"
})


# ================= BASIC HELPERS =================

def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def api_get(url, params=None):
    response = session.get(
        url,
        params=params,
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        if data.get("success") is False:
            raise RuntimeError(f"Ourbit API error: {data}")

        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            raise RuntimeError(f"Ourbit API code: {data}")

    return data


# ================= LBANK SNAPSHOTS =================

def load_lbank_prices():
    now = datetime.now(timezone.utc)

    latest = {}
    valid = stale = invalid = 0

    if not DATA_FILE.exists():
        return {}, valid, stale, invalid

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)

                symbol = str(
                    item.get("symbol", "")
                ).upper()

                price = number(item.get("lastPrice"))

                if not symbol.endswith("USDT") or price <= 0:
                    invalid += 1
                    continue

                stamp = datetime.fromisoformat(
                    str(item["timestamp"]).replace(
                        "Z", "+00:00"
                    )
                )

                if stamp.tzinfo is None:
                    stamp = stamp.replace(
                        tzinfo=timezone.utc
                    )

                age = (now - stamp).total_seconds()

                if age < 0 or age > MAX_LBANK_AGE:
                    stale += 1
                    continue

                valid += 1

                old = latest.get(symbol)

                if old is None or stamp > old["stamp"]:
                    latest[symbol] = {
                        "price": price,
                        "turnover": number(
                            item.get("turnover24h")
                        ),
                        "stamp": stamp,
                    }

            except Exception:
                invalid += 1

    return latest, valid, stale, invalid


# ================= OURBIT FUTURES KLINES =================

def ourbit_symbol(lbank_symbol):
    """
    مثال:
    BTCUSDT -> BTC_USDT
    ETHUSDT -> ETH_USDT

    قراردادهای خاص مثل 1000SHIBUSDT
    فقط در صورت وجود قرارداد هم‌نام در Ourbit قابل استفاده‌اند.
    """

    symbol = lbank_symbol.upper()

    if not symbol.endswith("USDT"):
        raise ValueError("Not a USDT contract")

    base = symbol[:-4]

    if not base:
        raise ValueError("Invalid symbol")

    return base + "_USDT"


def normalize_timestamp(value):
    stamp = int(float(value))

    # تبدیل ثانیه به میلی‌ثانیه
    if stamp < 10_000_000_000:
        stamp *= 1000

    return stamp


def parse_ourbit_candles(payload):
    """
    پشتیبانی از ساختارهای متداول پاسخ Kline:
    data.time/open/high/low/close/vol
    یا فهرست ردیف‌های کندل.

    اگر ساختار ناشناخته باشد، خطا می‌دهد؛
    داده حدسی وارد تحلیل نمی‌شود.
    """

    data = payload

    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    candles = []

    # ساختار ستونی:
    # {
    #   time: [],
    #   open: [],
    #   high: [],
    #   low: [],
    #   close: [],
    #   vol: []
    # }
    if isinstance(data, dict):

        time_values = data.get("time")

        if time_values is None:
            time_values = data.get("times")

        if isinstance(time_values, list):

            opens = data.get("open", [])
            highs = data.get("high", [])
            lows = data.get("low", [])
            closes = data.get("close", [])

            volumes = data.get("vol")
            if volumes is None:
                volumes = data.get("volume", [])

            lengths = [
                len(time_values),
                len(opens),
                len(highs),
                len(lows),
                len(closes),
                len(volumes),
            ]

            count = min(lengths)

            for i in range(count):
                candles.append({
                    "open_time": normalize_timestamp(
                        time_values[i]
                    ),
                    "open": number(opens[i]),
                    "high": number(highs[i]),
                    "low": number(lows[i]),
                    "close": number(closes[i]),
                    "volume": number(volumes[i]),
                })

        # ساختار فهرست داخل data
        elif isinstance(data.get("list"), list):
            data = data["list"]

        elif isinstance(data.get("rows"), list):
            data = data["rows"]

        else:
            raise ValueError(
                "Unknown Ourbit candle response structure"
            )

    if isinstance(data, list):

        for row in data:

            if isinstance(row, dict):
                stamp = (
                    row.get("time")
                    or row.get("openTime")
                    or row.get("timestamp")
                )

                if stamp is None:
                    raise ValueError(
                        "Ourbit candle timestamp missing"
                    )

                candles.append({
                    "open_time": normalize_timestamp(stamp),
                    "open": number(row.get("open")),
                    "high": number(row.get("high")),
                    "low": number(row.get("low")),
                    "close": number(row.get("close")),
                    "volume": number(
                        row.get("vol", row.get("volume"))
                    ),
                })

            elif isinstance(row, (list, tuple)) and len(row) >= 6:
                # قالب ردیفی متداول:
                # time, open, high, low, close, volume

                candles.append({
                    "open_time": normalize_timestamp(row[0]),
                    "open": number(row[1]),
                    "high": number(row[2]),
                    "low": number(row[3]),
                    "close": number(row[4]),
                    "volume": number(row[5]),
                })

            else:
                raise ValueError(
                    "Unknown Ourbit candle row format"
                )

    if not candles:
        raise ValueError("Ourbit returned no candles")

    return candles


def get_candles(symbol, interval_key):
    if interval_key not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")

    interval, interval_ms = TIMEFRAMES[interval_key]

    contract = ourbit_symbol(symbol)

    now_ms = int(time.time() * 1000)

    start_ms = now_ms - (
        interval_ms * (CANDLE_LIMIT + 3)
    )

    url = (
        f"{OURBIT_BASE}/api/v1/contract/kline/"
        f"{contract}"
    )

    payload = api_get(
        url,
        {
            "interval": interval,
            "start": start_ms,
            "end": now_ms,
        },
    )

    raw_candles = parse_ourbit_candles(payload)

    # مرتب‌سازی و حذف کندل تکراری
    unique = {}

    for candle in raw_candles:
        unique[candle["open_time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda x: x["open_time"]
    )

    # فقط کندل‌هایی که زمان بسته‌شدنشان گذشته باشد
    closed = []

    for candle in candles:

        if (
            candle["open"] <= 0
            or candle["high"] <= 0
            or candle["low"] <= 0
            or candle["close"] <= 0
            or candle["high"] < candle["low"]
        ):
            continue

        close_time = (
            candle["open_time"] + interval_ms
        )

        if close_time <= now_ms:
            candle["close_time"] = close_time
            closed.append(candle)

    if len(closed) < 220:
        raise ValueError(
            f"Not enough closed Ourbit candles: "
            f"{symbol} {interval_key} "
            f"({len(closed)})"
        )

    return closed[-CANDLE_LIMIT:]


# ================= INDICATORS =================

def ema(values, period):
    if len(values) < period:
        raise ValueError("Insufficient EMA data")

    multiplier = 2.0 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) <= period:
        raise ValueError("Insufficient RSI data")

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

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
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def macd(values):
    if len(values) < 60:
        raise ValueError("Insufficient MACD data")

    multiplier12 = 2 / 13
    multiplier26 = 2 / 27

    e12 = sum(values[:12]) / 12
    e26 = sum(values[:26]) / 26

    line = []

    for i, value in enumerate(values):

        if i >= 12:
            e12 = (
                value * multiplier12
                + e12 * (1 - multiplier12)
            )

        if i >= 26:
            e26 = (
                value * multiplier26
                + e26 * (1 - multiplier26)
            )

            line.append(e12 - e26)

    if len(line) < 10:
        raise ValueError("Insufficient MACD signal data")

    signal = ema(line, 9)

    return (
        line[-1],
        signal,
        line[-1] - signal,
    )


def atr(candles, period=14):
    if len(candles) < period + 1:
        raise ValueError("Insufficient ATR data")

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous_close = candles[i - 1]["close"]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        )

        true_ranges.append(tr)

    value = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        value = (
            value * (period - 1) + tr
        ) / period

    return value


def indicators(candles):
    closes = [x["close"] for x in candles]
    volumes = [x["volume"] for x in candles]

    m_line, m_signal, m_hist = macd(closes)

    return {
        "close": closes[-1],
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema200": ema(closes, 200),
        "rsi": rsi(closes),
        "macd": m_line,
        "macd_signal": m_signal,
        "macd_hist": m_hist,
        "atr": atr(candles),
        "volume": volumes[-1],
        "volume_avg": (
            sum(volumes[-21:-1]) / 20
        ),
    }


# ================= STRATEGY =================

def analyze_symbol(symbol, lbank_price):

    market = {}

    for key in TIMEFRAMES:
        market[key] = get_candles(symbol, key)
        time.sleep(0.05)

    # مقایسه آخرین کندل بسته‌شده Ourbit با LBank
    reference_price = market["1m"][-1]["close"]

    divergence = (
        abs(reference_price - lbank_price)
        / lbank_price
    )

    if divergence > MAX_PRICE_DIVERGENCE:
        return None, (
            f"Price divergence "
            f"{divergence * 100:.3f}%"
        )

    ind = {
        key: indicators(value)
        for key, value in market.items()
    }

    # روند تایم‌فریم بالاتر
    long_htf = (
        ind["4h"]["close"] > ind["4h"]["ema20"]
        > ind["4h"]["ema50"]
        and ind["4h"]["close"] > ind["4h"]["ema200"]
        and ind["1h"]["close"] > ind["1h"]["ema20"]
        > ind["1h"]["ema50"]
        and ind["1h"]["close"] > ind["1h"]["ema200"]
    )

    short_htf = (
        ind["4h"]["close"] < ind["4h"]["ema20"]
        < ind["4h"]["ema50"]
        and ind["4h"]["close"] < ind["4h"]["ema200"]
        and ind["1h"]["close"] < ind["1h"]["ema20"]
        < ind["1h"]["ema50"]
        and ind["1h"]["close"] < ind["1h"]["ema200"]
    )

    setup = ind["15m"]
    structure = ind["5m"]
    entry_ind = ind["1m"]

    long_setup = (
        setup["macd_hist"] > 0
        and setup["rsi"] > 50
        and setup["close"] > setup["ema20"]
    )

    short_setup = (
        setup["macd_hist"] < 0
        and setup["rsi"] < 50
        and setup["close"] < setup["ema20"]
    )

    # ساختار پنج دقیقه
    previous_5m = market["5m"][-6:-1]
    previous_1m = market["1m"][-6:-1]

    high_5m = max(x["high"] for x in previous_5m)
    low_5m = min(x["low"] for x in previous_5m)

    high_1m = max(x["high"] for x in previous_1m)
    low_1m = min(x["low"] for x in previous_1m)

    long_structure = structure["close"] > high_5m
    short_structure = structure["close"] < low_5m

    volume_ok = (
        entry_ind["volume_avg"] > 0
        and entry_ind["volume"]
        >= entry_ind["volume_avg"] * 1.2
    )

    long_entry = (
        entry_ind["close"] > high_1m
        and entry_ind["rsi"] > 50
        and entry_ind["rsi"] < 70
        and entry_ind["macd_hist"] > 0
        and volume_ok
    )

    short_entry = (
        entry_ind["close"] < low_1m
        and entry_ind["rsi"] < 50
        and entry_ind["rsi"] > 30
        and entry_ind["macd_hist"] < 0
        and volume_ok
    )

    side = None

    if (
        long_htf
        and long_setup
        and long_structure
        and long_entry
    ):
        side = "LONG"

    elif (
        short_htf
        and short_setup
        and short_structure
        and short_entry
    ):
        side = "SHORT"

    if side is None:
        return None, "Strategy conditions not aligned"

    entry = entry_ind["close"]
    current_atr = entry_ind["atr"]

    if current_atr <= 0:
        return None, "Invalid ATR"

    if side == "LONG":

        structural_stop = min(
            x["low"] for x in market["5m"][-5:]
        )

        stop = min(
            structural_stop,
            entry - current_atr * 1.2
        )

        risk = entry - stop
        target = entry + risk * TARGET_RR

    else:

        structural_stop = max(
            x["high"] for x in market["5m"][-5:]
        )

        stop = max(
            structural_stop,
            entry + current_atr * 1.2
        )

        risk = stop - entry
        target = entry - risk * TARGET_RR

    if risk <= 0 or target <= 0:
        return None, "Invalid risk or target"

    trading_cost = entry * 2 * (
        FEE_PER_SIDE + SLIPPAGE_PER_SIDE
    )

    if side == "LONG":
        gross_reward = target - entry
    else:
        gross_reward = entry - target

    net_reward = gross_reward - trading_cost
    net_risk = risk + trading_cost

    net_rr = (
        net_reward / net_risk
        if net_risk > 0 else 0
    )

    if net_rr < MIN_NET_RR:
        return None, f"Net RR too low: {net_rr:.2f}"

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "net_rr": net_rr,
        "divergence": divergence,
        "rsi_1m": entry_ind["rsi"],
        "rsi_15m": setup["rsi"],
        "volume_ratio": (
            entry_ind["volume"]
            / entry_ind["volume_avg"]
        ),
    }, None


# ================= MAIN =================

def main():

    now = datetime.now(timezone.utc)

    snapshots, valid, stale, invalid = (
        load_lbank_prices()
    )

    signals = []
    errors = []
    rejected = 0

    candidates = []

    for symbol, item in snapshots.items():

        candidates.append((
            symbol,
            item["price"],
            item["turnover"],
        ))

    candidates.sort(
        key=lambda x: x[2],
        reverse=True,
    )

    candidates = candidates[:MAX_SYMBOLS]

    checked = 0

    for symbol, lbank_price, turnover in candidates:

        checked += 1

        try:
            signal, reason = analyze_symbol(
                symbol,
                lbank_price,
            )

            if signal:
                signal["lbank_price"] = lbank_price
                signal["turnover"] = turnover
                signals.append(signal)

            else:
                rejected += 1

        except requests.HTTPError as error:
            errors.append(
                f"{symbol}: Ourbit HTTP error: {error}"
            )

        except Exception as error:
            errors.append(
                f"{symbol}: {type(error).__name__}: {error}"
            )

    signals.sort(
        key=lambda x: x["net_rr"],
        reverse=True,
    )

    signals = signals[:MAX_SIGNALS]

    lines = [
        "🎯 LBANK FUTURES SNIPER",
        "MODE: SNIPER BALANCED",
        "",
        (
            "🟢 STATUS: SIGNALS FOUND"
            if signals
            else "🔴 STATUS: ENTRY DISABLED"
        ),
        "",
        "📊 MARKET DATA",
        "CANDLE SOURCE: OURBIT USDT-M FUTURES",
        "PRICE CHECK: LBANK FUTURES",
        f"Valid LBank snapshots: {valid}",
        f"Stale snapshots: {stale}",
        f"Invalid records: {invalid}",
        f"Symbols analyzed: {checked}",
        f"Rejected setups: {rejected}",
        f"Data/API errors: {len(errors)}",
        "",
        "📈 STRATEGY",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry: 1m",
        "Indicators: EMA20/50/200, RSI, MACD, ATR",
        "Volume confirmation: 1.2x average",
        f"Minimum net RR: {MIN_NET_RR}",
        "",
        "⚠️ DATA VALIDATION",
        "Ourbit Futures candles only.",
        "Spot candles are not used.",
        "LBank price divergence validation enabled.",
        (
            "Maximum allowed divergence: "
            f"{MAX_PRICE_DIVERGENCE * 100:.2f}%"
        ),
        "",
    ]

    if signals:

        for index, signal in enumerate(
            signals,
            start=1
        ):

            lines.extend([
                (
                    f"#{index} {signal['symbol']} - "
                    f"{signal['side']}"
                ),
                f"Entry: {signal['entry']:.8g}",
                f"Stop Loss: {signal['stop']:.8g}",
                f"Take Profit: {signal['target']:.8g}",
                f"Net RR (estimated): {signal['net_rr']:.2f}",
                f"RSI 1m: {signal['rsi_1m']:.1f}",
                f"RSI 15m: {signal['rsi_15m']:.1f}",
                (
                    f"Volume ratio: "
                    f"{signal['volume_ratio']:.2f}"
                ),
                (
                    "Ourbit/LBank divergence: "
                    f"{signal['divergence'] * 100:.3f}%"
                ),
                "",
            ])

    else:
        lines.append(
            "No confirmed setup. ورود نکن."
        )

    if errors:

        lines.extend([
            "",
            "⚠️ DATA / API WARNINGS",
        ])

        for error in errors[:8]:
            lines.append(str(error)[:250])

    lines.extend([
        "",
        "🔒 SAFETY",
        "Real orders: DISABLED",
        "Signals only - manual verification required.",
        "High leverage risk: VERY HIGH",
        "",
        "UTC: " + now.strftime("%Y-%m-%d %H:%M:%S"),
    ])

    report = "\n".join(lines)

    REPORT_FILE.write_text(
        report + "\n",
        encoding="utf-8"
    )

    print(report)


if __name__ == "__main__":
    main()
