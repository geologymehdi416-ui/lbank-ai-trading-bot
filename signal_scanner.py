
import os
import json
import time
import requests
import numpy as np
import pandas as pd

DATA_FILE = "data/prices.jsonl"

TIMEFRAMES = {
    "4h": "4h",
    "1h": "1h",
    "15m": "15m",
    "5m": "5m",
    "1m": "1m",
}

MIN_RR = 1.5
MAX_SIGNALS = 5
MAX_SYMBOLS = 30

# Candle endpoint is intentionally configurable.
# Do not point this to a Spot or Binance endpoint.
LBANK_FUTURES_CANDLE_URL = os.getenv(
    "LBANK_FUTURES_CANDLE_URL", ""
)

session = requests.Session()
session.headers.update({
    "User-Agent": "LBank-Futures-Scanner/1.0"
})


def load_symbols():
    if not os.path.exists(DATA_FILE):
        return []

    latest = {}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")
                if symbol:
                    latest[symbol] = item
            except json.JSONDecodeError:
                continue

    return sorted(
        latest.values(),
        key=lambda x: x.get("quote_volume") or 0,
        reverse=True
    )[:MAX_SYMBOLS]


def fetch_candles(symbol, timeframe, limit=250):
    if not LBANK_FUTURES_CANDLE_URL:
        raise RuntimeError(
            "LBank Futures candle endpoint is not configured. "
            "No Binance or Spot fallback is allowed."
        )

    response = session.get(
        LBANK_FUTURES_CANDLE_URL,
        params={
            "symbol": symbol,
            "interval": timeframe,
            "limit": limit,
        },
        timeout=15,
    )
    response.raise_for_status()

    payload = response.json()

    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("result")
    else:
        rows = payload

    if not isinstance(rows, list) or len(rows) < 100:
        raise ValueError(f"Invalid candles for {symbol} {timeframe}")

    df = pd.DataFrame(rows)

    # Expected normalized candle columns:
    # timestamp, open, high, low, close, volume
    required = [
        "timestamp", "open", "high",
        "low", "close", "volume"
    ]

    if not all(col in df.columns for col in required):
        raise ValueError(
            "Candle response must be normalized to: "
            + ", ".join(required)
        )

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)
    df = df.sort_values("timestamp").reset_index(drop=True)

    if len(df) < 100:
        raise ValueError("Not enough valid candles")

    return df


def indicators(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

    df["ema100"] = df["close"].ewm(
        span=100, adjust=False
    ).mean()

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = df["close"].ewm(
        span=12, adjust=False
    ).mean()

    ema26 = df["close"].ewm(
        span=26, adjust=False
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9, adjust=False
    ).mean()

    df["macd_hist"] = (
        df["macd"] - df["macd_signal"]
    )

    previous_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1/14, adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(20).mean()

    return df.dropna().reset_index(drop=True)


def market_trend(df):
    last = df.iloc[-1]

    if (
        last["close"] > last["ema20"]
        and last["ema20"] > last["ema50"]
        and last["ema50"] > last["ema100"]
    ):
        return "LONG"

    if (
        last["close"] < last["ema20"]
        and last["ema20"] < last["ema50"]
        and last["ema50"] < last["ema100"]
    ):
        return "SHORT"

    return "NEUTRAL"


def structure(df):
    recent = df.iloc[-21:-1]

    high = recent["high"].max()
    low = recent["low"].min()

    close = df.iloc[-1]["close"]

    if close > high:
        return "BULLISH_BREAKOUT"

    if close < low:
        return "BEARISH_BREAKDOWN"

    return "RANGE"


def analyze_symbol(symbol):
    try:
        candles = {}

        for tf in TIMEFRAMES:
            candles[tf] = indicators(
                fetch_candles(symbol, tf)
            )
            time.sleep(0.1)

        h4 = market_trend(candles["4h"])
        h1 = market_trend(candles["1h"])
        m15 = candles["15m"]
        m5 = candles["5m"]
        m1 = candles["1m"]

        if h4 != h1 or h4 == "NEUTRAL":
            return None

        trend = h4

        last15 = m15.iloc[-1]
        last5 = m5.iloc[-1]
        last1 = m1.iloc[-1]

        structure5 = structure(m5)

        volume_ok = (
            last1["volume_avg"] > 0
            and last1["volume"] > last1["volume_avg"] * 1.2
        )

        if trend == "LONG":
            setup_ok = (
                last15["close"] > last15["ema50"]
                and last15["macd_hist"] > 0
                and 50 < last15["rsi"] < 72
                and structure5 == "BULLISH_BREAKOUT"
                and last5["close"] > last5["ema20"]
                and last1["close"] > last1["ema20"]
                and last1["macd_hist"] > 0
                and volume_ok
            )

            direction = "LONG"

        else:
            setup_ok = (
                last15["close"] < last15["ema50"]
                and last15["macd_hist"] < 0
                and 28 < last15["rsi"] < 50
                and structure5 == "BEARISH_BREAKDOWN"
                and last5["close"] < last5["ema20"]
                and last1["close"] < last1["ema20"]
                and last1["macd_hist"] < 0
                and volume_ok
            )

            direction = "SHORT"

        if not setup_ok:
            return None

        entry = float(last1["close"])
        atr = float(last1["atr"])

        if not np.isfinite(atr) or atr <= 0:
            return None

        if direction == "LONG":
            swing_low = float(m5["low"].iloc[-10:].min())
            sl = min(swing_low, entry - 1.2 * atr)

            risk = entry - sl
            tp = entry + risk * MIN_RR

        else:
            swing_high = float(m5["high"].iloc[-10:].max())
            sl = max(swing_high, entry + 1.2 * atr)

            risk = sl - entry
            tp = entry - risk * MIN_RR

        if risk <= 0 or tp <= 0:
            return None

        rr = abs(tp - entry) / risk

        if rr < MIN_RR:
            return None

        return {
            "symbol": symbol,
            "direction": direction,
            "entry": round(entry, 8),
            "stop_loss": round(sl, 8),
            "take_profit": round(tp, 8),
            "rr": round(rr, 2),
            "rsi_15m": round(float(last15["rsi"]), 2),
            "atr_1m": round(atr, 8),
            "trend_4h": h4,
            "trend_1h": h1,
            "structure_5m": structure5,
            "volume_ratio": round(
                float(last1["volume"] / last1["volume_avg"]), 2
            ),
        }

    except Exception as exc:
        print(f"{symbol}: {exc}")
        return None


def main():
    symbols = load_symbols()

    print(f"Loaded {len(symbols)} Futures symbols")

    signals = []

    for item in symbols:
        symbol = item["symbol"]

        result = analyze_symbol(symbol)

        if result:
            signals.append(result)
            print("SIGNAL:", result)

    signals.sort(
        key=lambda x: x["rr"],
        reverse=True
    )

    signals = signals[:MAX_SIGNALS]

    os.makedirs("data", exist_ok=True)

    with open(
        "data/signals.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            signals,
            f,
            ensure_ascii=False,
            indent=2
        )

    if not signals:
        print("NO SETUP - DO NOT ENTER")

    print(f"Final signals: {len(signals)}")


if __name__ == "__main__":
    main()
