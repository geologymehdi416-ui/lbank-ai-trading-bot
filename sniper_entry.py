
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import time
import requests

from pathlib import Path
from datetime import datetime, timezone

# ================= CONFIG =================

DATA_FILE = Path("data/prices.jsonl")
REPORT_FILE = Path("report.txt")

BASE = "https://fapi.binance.com"

TIMEFRAMES = {
    "4h": "4h",
    "1h": "1h",
    "15m": "15m",
    "5m": "5m",
    "1m": "1m",
}

MAX_SYMBOLS = 30
MAX_SIGNALS = 5
MAX_DIVERGENCE = 0.0020

MIN_NET_RR = 1.5
TARGET_RR = 1.8

FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002

session = requests.Session()
session.headers.update({"User-Agent": "LBank-Futures-Sniper/4.0"})

# ================= HTTP =================

def api(path, params=None):
    response = session.get(
        BASE + path,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    return response.json()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


# ================= LBank DATA =================

def load_lbank():
    prices = {}

    if not DATA_FILE.exists():
        return prices

    with DATA_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                symbol = str(item.get("symbol", "")).upper()
                price = number(item.get("lastPrice"))

                if (
                    symbol.endswith("USDT")
                    and price
                    and price > 0
                    and item.get("source") == "LBank Futures"
                ):
                    prices[symbol] = {
                        "price": price,
                        "turnover": number(item.get("turnover24h")) or 0
                    }
            except Exception:
                continue

    return prices


# ================= INDICATORS =================

def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)

    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

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
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_hist(values):
    if len(values) < 35:
        return None

    fast = []
    slow = []

    for i in range(len(values)):
        fast.append(ema(values[:i + 1], 12))
        slow.append(ema(values[:i + 1], 26))

    macd_values = [
        a - b for a, b in zip(fast, slow)
        if a is not None and b is not None
    ]

    if len(macd_values) < 9:
        return None

    signal = ema(macd_values, 9)

    if signal is None:
        return None

    return macd_values[-1] - signal


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None

    trs = []

    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    return sum(trs[-period:]) / period


# ================= BINANCE FUTURES =================

def get_symbols(lbank):
    info = api("/fapi/v1/exchangeInfo")

    allowed = {
        item["symbol"]
        for item in info.get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
        and item.get("marginAsset") == "USDT"
    }

    candidates = [
        (symbol, data["turnover"])
        for symbol, data in lbank.items()
        if symbol in allowed
    ]

    candidates.sort(key=lambda x: x[1], reverse=True)

    return [symbol for symbol, _ in candidates[:MAX_SYMBOLS]]


def get_candles(symbol, interval):
    raw = api(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": 120
        }
    )

    now_ms = int(time.time() * 1000)
    candles = []

    for row in raw:
        # Only fully closed candles are accepted.
        if int(row[6]) >= now_ms:
            continue

        values = [
            number(row[1]),
            number(row[2]),
            number(row[3]),
            number(row[4]),
            number(row[5])
        ]

        if any(v is None for v in values):
            continue

        candles.append({
            "open": values[0],
            "high": values[1],
            "low": values[2],
            "close": values[3],
            "volume": values[4],
        })

    if len(candles) < 60:
        raise ValueError("Not enough closed candles: " + interval)

    return candles


# ================= STRATEGY =================

def trend(candles):
    closes = [x["close"] for x in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 100)

    if None in (e20, e50, e200):
        return None

    price = closes[-1]

    if price > e20 > e50 and price > e200:
        return "LONG"

    if price < e20 < e50 and price < e200:
        return "SHORT"

    return None


def analyze(symbol, lbank_price):
    candles = {
        tf: get_candles(symbol, interval)
        for tf, interval in TIMEFRAMES.items()
    }

    # Compare current Binance Futures ticker with LBank snapshot.
    ticker = api(
        "/fapi/v1/ticker/price",
        {"symbol": symbol}
    )

    binance_price = number(ticker.get("price"))

    if not binance_price or binance_price <= 0:
        return None, "Invalid Binance price"

    divergence = abs(lbank_price - binance_price) / binance_price

    if divergence > MAX_DIVERGENCE:
        return None, "LBank/Binance price divergence"

    # Higher timeframe alignment.
    t4h = trend(candles["4h"])
    t1h = trend(candles["1h"])

    if not t4h or t4h != t1h:
        return None, "HTF trend not aligned"

    direction = t4h

    # 15m setup.
    c15 = candles["15m"]
    close15 = [x["close"] for x in c15]
    rsi15 = rsi(close15)
    macd15 = macd_hist(close15)

    if rsi15 is None or macd15 is None:
        return None, "15m indicators unavailable"

    e20_15 = ema(close15, 20)
    e50_15 = ema(close15, 50)

    if direction == "LONG":
        setup_ok = (
            close15[-1] > e20_15 > e50_15
            and 50 < rsi15 < 72
            and macd15 > 0
        )
    else:
        setup_ok = (
            close15[-1] < e20_15 < e50_15
            and 28 < rsi15 < 50
            and macd15 < 0
        )

    if not setup_ok:
        return None, "15m setup not confirmed"

    # 5m market structure breakout.
    c5 = candles["5m"]
    previous5 = c5[-21:-1]
    close5 = c5[-1]["close"]

    if direction == "LONG":
        structure_ok = close5 > max(x["high"] for x in previous5)
    else:
        structure_ok = close5 < min(x["low"] for x in previous5)

    if not structure_ok:
        return None, "5m structure breakout missing"

    # 1m entry confirmation.
    c1 = candles["1m"]
    closes = [x["close"] for x in c1]
    volumes = [x["volume"] for x in c1]

    current = c1[-1]
    previous = c1[-11:-1]

    rsi1 = rsi(closes)
    macd1 = macd_hist(closes)
    atr1 = atr(
        [x["high"] for x in c1],
        [x["low"] for x in c1],
        closes
    )

    if None in (rsi1, macd1, atr1) or atr1 <= 0:
        return None, "1m indicators unavailable"

    avg_volume = sum(volumes[-21:-1]) / 20

    if avg_volume <= 0:
        return None, "Invalid volume"

    volume_ok = current["volume"] > avg_volume * 1.2

    if direction == "LONG":
        entry_ok = (
            current["close"] > max(x["high"] for x in previous)
            and 52 < rsi1 < 72
            and macd1 > 0
            and volume_ok
        )
    else:
        entry_ok = (
            current["close"] < min(x["low"] for x in previous)
            and 28 < rsi1 < 48
            and macd1 < 0
            and volume_ok
        )

    if not entry_ok:
        return None, "1m entry not confirmed"

    entry = current["close"]
    stop_distance = atr1 * 1.5

    if direction == "LONG":
        stop = entry - stop_distance
        target = entry + stop_distance * TARGET_RR
    else:
        stop = entry + stop_distance
        target = entry - stop_distance * TARGET_RR

    # Conservative round-trip cost estimate.
    cost_rate = 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
    cost = entry * cost_rate

    gross_risk = abs(entry - stop)
    gross_reward = abs(target - entry)

    net_risk = gross_risk + cost
    net_reward = gross_reward - cost

    if net_reward <= 0:
        return None, "Net reward is not positive"

    net_rr = net_reward / net_risk

    if net_rr < MIN_NET_RR:
        return None, "Net RR below minimum"

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "net_rr": net_rr,
        "rsi1": rsi1,
        "rsi15": rsi15,
        "macd1": macd1,
        "volume_ratio": current["volume"] / avg_volume,
        "divergence": divergence * 100,
    }, None


# ================= REPORT =================

def main():
    now = datetime.now(timezone.utc)
    lines = [
        "🎯 LBank FUTURES SNIPER",
        "MODE: SNIPER BALANCED",
        "",
        "DATA: Binance USD-M Futures OHLCV",
        "PRICE CHECK: LBank Futures snapshots",
        "Candles are NOT LBank-native candles.",
        "Real orders: DISABLED",
        ""
    ]

    lbank = load_lbank()
    valid = len(lbank)

    signals = []
    rejected = 0
    errors = 0

    try:
        symbols = get_symbols(lbank)
    except Exception as exc:
        symbols = []
        errors += 1
        lines.append("Exchange info error: " + str(exc)[:160])

    for symbol in symbols:
        if len(signals) >= MAX_SIGNALS:
            break

        try:
            result, reason = analyze(
                symbol,
                lbank[symbol]["price"]
            )

            if result:
                signals.append(result)
            else:
                rejected += 1

        except Exception as exc:
            errors += 1
            print("Symbol error:", symbol, str(exc)[:160])

    lines += [
        "📊 MARKET DATA",
        f"LBank valid snapshots: {valid}",
        f"Binance symbols checked: {len(symbols)}",
        f"Rejected setups: {rejected}",
        f"Data/API errors: {errors}",
        "",
        "📈 STRATEGY",
        "HTF: 4H / 1H",
        "Setup: 15m",
        "Structure: 5m",
        "Entry: 1m",
        f"Minimum net RR: {MIN_NET_RR}",
        f"Maximum signals: {MAX_SIGNALS}",
        "",
    ]

    if signals:
        lines.append("🟢 CONFIRMED SIGNALS")

        for i, s in enumerate(signals, 1):
            lines += [
                "",
                f"{i}. {s['symbol']} — {s['direction']}",
                f"Entry: {s['entry']:.8g}",
                f"Stop Loss: {s['stop']:.8g}",
                f"Take Profit: {s['target']:.8g}",
                f"Net RR: {s['net_rr']:.2f}",
                f"RSI 1m: {s['rsi1']:.1f}",
                f"RSI 15m: {s['rsi15']:.1f}",
                f"MACD 1m: {s['macd1']:.8g}",
                f"Volume ratio: {s['volume_ratio']:.2f}x",
                f"LBank/Binance divergence: {s['divergence']:.3f}%",
            ]
    else:
        lines += [
            "🔴 STATUS: NO VALID ENTRY",
            "No setup passed all confirmation filters.",
            "نتیجه: ورود نکن",
        ]

    lines += [
        "",
        "⚠️ Risk note:",
        "Fees/slippage are estimates, not guaranteed costs.",
        "High leverage increases liquidation risk.",
        "Signals are informational; no orders are placed.",
        "",
        "UTC: " + now.strftime("%Y-%m-%d %H:%M:%S"),
    ]

    report = "\n".join(lines)
    REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
