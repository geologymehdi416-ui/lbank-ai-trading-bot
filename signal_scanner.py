
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

BINANCE_BASE = "https://fapi.binance.com"

MAX_LBANK_AGE = 600
MAX_PRICE_DIVERGENCE = 0.002       # 0.20%
MIN_NET_RR = 1.5
TARGET_RR = 1.8
MAX_SYMBOLS = 12

# فرض هزینه؛ با کارمزد واقعی حساب خودت تنظیم کن.
FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002

TIMEFRAMES = {
    "4h": "4h",
    "1h": "1h",
    "15m": "15m",
    "5m": "5m",
    "1m": "1m",
}

session = requests.Session()
session.headers.update({"User-Agent": "LBank-Futures-Sniper/1.0"})


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


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
                symbol = str(item.get("symbol", "")).upper()
                price = number(item.get("lastPrice"))

                if not symbol.endswith("USDT") or price <= 0:
                    invalid += 1
                    continue

                stamp = datetime.fromisoformat(
                    str(item["timestamp"]).replace("Z", "+00:00")
                )

                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)

                age = (now - stamp).total_seconds()

                if age < 0 or age > MAX_LBANK_AGE:
                    stale += 1
                    continue

                valid += 1
                old = latest.get(symbol)

                if old is None or stamp > old["stamp"]:
                    latest[symbol] = {
                        "price": price,
                        "turnover": number(item.get("turnover24h")),
                        "stamp": stamp,
                    }

            except Exception:
                invalid += 1

    return latest, valid, stale, invalid


def api_get(path, params=None):
    response = session.get(
        BINANCE_BASE + path,
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("code") is not None:
        raise RuntimeError(str(data))

    return data


def get_binance_symbols():
    data = api_get("/fapi/v1/exchangeInfo")
    result = set()

    for item in data.get("symbols", []):
        if (
            item.get("status") == "TRADING"
            and item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
        ):
            result.add(item["symbol"])

    return result


def get_candles(symbol, interval, limit=120):
    rows = api_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
    )

    now_ms = int(time.time() * 1000)
    candles = []

    for row in rows:
        # فقط کندل بسته‌شده
        if int(row[6]) >= now_ms:
            continue

        candle = {
            "open_time": int(row[0]),
            "open": number(row[1]),
            "high": number(row[2]),
            "low": number(row[3]),
            "close": number(row[4]),
            "volume": number(row[5]),
            "close_time": int(row[6]),
        }

        if (
            candle["high"] <= 0
            or candle["low"] <= 0
            or candle["close"] <= 0
            or candle["high"] < candle["low"]
        ):
            continue

        candles.append(candle)

    if len(candles) < 60:
        raise ValueError(
            f"Not enough closed candles: {symbol} {interval}"
        )

    return candles


def ema(values, period):
    if len(values) < period:
        raise ValueError("Insufficient EMA data")

    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)

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
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values):
    # MACD line = EMA12 - EMA26
    # Signal = EMA9 of MACD line
    if len(values) < 60:
        raise ValueError("Insufficient MACD data")

    multiplier12 = 2 / 13
    multiplier26 = 2 / 27

    e12 = sum(values[:12]) / 12
    e26 = sum(values[:26]) / 26
    line = []

    for i, value in enumerate(values):
        if i >= 12:
            e12 = value * multiplier12 + e12 * (1 - multiplier12)
        if i >= 26:
            e26 = value * multiplier26 + e26 * (1 - multiplier26)
            line.append(e12 - e26)

    if len(line) < 10:
        raise ValueError("Insufficient MACD signal data")

    signal = ema(line, 9)
    return line[-1], signal, line[-1] - signal


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
        value = (value * (period - 1) + tr) / period

    return value


def indicators(candles):
    closes = [x["close"] for x in candles]
    volumes = [x["volume"] for x in candles]

    m_line, m_signal, m_hist = macd(closes)

    return {
        "close": closes[-1],
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "rsi": rsi(closes),
        "macd": m_line,
        "macd_signal": m_signal,
        "macd_hist": m_hist,
        "atr": atr(candles),
        "volume": volumes[-1],
        "volume_avg": sum(volumes[-21:-1]) / 20,
    }


def analyze_symbol(symbol, lbank_price):
    market = {}

    for key, interval in TIMEFRAMES.items():
        market[key] = get_candles(symbol, interval)

    # تطبیق قیمت آخرین کندل بسته‌شده با قیمت LBank
    reference_price = market["1m"][-1]["close"]

    divergence = abs(
        reference_price - lbank_price
    ) / lbank_price

    if divergence > MAX_PRICE_DIVERGENCE:
        return None, (
            f"Price divergence {divergence * 100:.3f}%"
        )

    ind = {
        key: indicators(value)
        for key, value in market.items()
    }

    long_htf = (
        ind["4h"]["close"] > ind["4h"]["ema20"]
        > ind["4h"]["ema50"]
        and ind["1h"]["close"] > ind["1h"]["ema20"]
        > ind["1h"]["ema50"]
    )

    short_htf = (
        ind["4h"]["close"] < ind["4h"]["ema20"]
        < ind["4h"]["ema50"]
        and ind["1h"]["close"] < ind["1h"]["ema20"]
        < ind["1h"]["ema50"]
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

    # شکست سقف/کف پنج کندل بسته‌شده قبلی
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
        and entry_ind["volume"] >= entry_ind["volume_avg"] * 1.2
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

    if long_htf and long_setup and long_structure and long_entry:
        side = "LONG"

    elif short_htf and short_setup and short_structure and short_entry:
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

    # هزینه تخمینی رفت و برگشت
    trading_cost = entry * 2 * (
        FEE_PER_SIDE + SLIPPAGE_PER_SIDE
    )

    if side == "LONG":
        gross_reward = target - entry
    else:
        gross_reward = entry - target

    net_reward = gross_reward - trading_cost
    net_risk = risk + trading_cost

    net_rr = net_reward / net_risk if net_risk > 0 else 0

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
            entry_ind["volume"] / entry_ind["volume_avg"]
        ),
    }, None


def main():
    now = datetime.now(timezone.utc)

    snapshots, valid, stale, invalid = load_lbank_prices()

    signals = []
    errors = []

    try:
        binance_symbols = get_binance_symbols()
    except Exception as error:
        binance_symbols = set()
        errors.append(f"Binance exchangeInfo error: {error}")

    candidates = []

    for symbol, item in snapshots.items():
        if symbol in binance_symbols:
            candidates.append((
                symbol,
                item["price"],
                item["turnover"],
            ))

    candidates.sort(key=lambda x: x[2], reverse=True)
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

        except requests.HTTPError as error:
            errors.append(f"{symbol}: HTTP {error}")

        except Exception as error:
            errors.append(f"{symbol}: {error}")

    signals.sort(
        key=lambda x: x["net_rr"],
        reverse=True,
    )
    signals = signals[:5]

    lines = [
        "🎯 LBank FUTURES SNIPER",
        "MODE: SNIPER BALANCED",
        "",
        (
            "🟢 STATUS: SIGNALS FOUND"
            if signals else
            "🔴 STATUS: ENTRY DISABLED"
        ),
        "",
        "📊 MARKET DATA SUMMARY",
        f"Valid LBank snapshots: {valid}",
        f"Stale snapshots: {stale}",
        f"Invalid records: {invalid}",
        f"Common Binance Futures symbols: {len(candidates)}",
        f"Symbols analyzed: {checked}",
        "",
        "📈 STRATEGY",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry: 1m",
        f"Minimum net RR: {MIN_NET_RR}",
        "",
        "⚠️ CANDLE SOURCE: BINANCE USD-M FUTURES",
        "LBank price is used for divergence validation.",
        "Candles are NOT verified LBank-native candles.",
        f"Maximum allowed divergence: {MAX_PRICE_DIVERGENCE * 100:.2f}%",
        "",
    ]

    if signals:
        for index, signal in enumerate(signals, start=1):
            lines.extend([
                f"#{index} {signal['symbol']} - {signal['side']}",
                f"Entry: {signal['entry']:.8g}",
                f"Stop Loss: {signal['stop']:.8g}",
                f"Take Profit: {signal['target']:.8g}",
                f"Net RR (estimated): {signal['net_rr']:.2f}",
                f"RSI 1m: {signal['rsi_1m']:.1f}",
                f"RSI 15m: {signal['rsi_15m']:.1f}",
                f"Volume ratio: {signal['volume_ratio']:.2f}",
                f"LBank/Binance divergence: {signal['divergence'] * 100:.3f}%",
                "",
            ])
    else:
        lines.append(
            "No fully confirmed setup. ورود نکن."
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
        "Spot candles: FORBIDDEN",
        "Real orders: DISABLED",
        "Signals only - manual verification required.",
        "High leverage risk: VERY HIGH",
        "",
        "UTC: " + now.strftime("%Y-%m-%d %H:%M:%S"),
    ])

    report = "\n".join(lines)
    REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
