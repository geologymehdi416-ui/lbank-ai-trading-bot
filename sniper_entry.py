
import os
import json
import time
import requests
import numpy as np
import pandas as pd

DATA_FILE = "data/prices.jsonl"
OUTPUT_FILE = "data/signals.json"

TIMEFRAMES = ["4h", "1h", "15m", "5m", "1m"]

MIN_RR = 1.5
MAX_SIGNALS = 5
MAX_SYMBOLS = 30

# Must be a verified LBank Futures candle endpoint.
# No Binance or Spot fallback is permitted.
CANDLE_URL = os.getenv("LBANK_FUTURES_CANDLE_URL", "")

session = requests.Session()
session.headers.update({
    "User-Agent": "LBank-Futures-Scanner/2.0",
    "Accept": "application/json",
})


def load_symbols():
    if not os.path.exists(DATA_FILE):
        raise RuntimeError("data/prices.jsonl not found")

    latest = {}

    with open(DATA_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                symbol = item.get("symbol")

                if symbol:
                    latest[symbol] = item

            except json.JSONDecodeError:
                continue

    symbols = list(latest.values())

    symbols.sort(
        key=lambda x: float(x.get("quote_volume") or 0),
        reverse=True
    )

    return symbols[:MAX_SYMBOLS]


def parse_candles(payload):
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise ValueError(f"API failure: {payload}")

        rows = payload.get("data")

        if rows is None:
            rows = payload.get("result")

        if rows is None:
            rows = payload.get("records")

    elif isinstance(payload, list):
        rows = payload

    else:
        rows = None

    if not isinstance(rows, list):
        raise ValueError("Unknown candle response format")

    if not rows:
        raise ValueError("Empty candle response")

    # Support documented normalized object format.
    if isinstance(rows[0], dict):
        df = pd.DataFrame(rows)

        aliases = {
            "timestamp": ["timestamp", "time", "ts"],
            "open": ["open", "o"],
            "high": ["high", "h"],
            "low": ["low", "l"],
            "close": ["close", "c"],
            "volume": ["volume", "vol", "v"],
        }

        rename = {}

        for target, candidates in aliases.items():
            for candidate in candidates:
                if candidate in df.columns:
                    rename[candidate] = target
                    break

        df = df.rename(columns=rename)

    else:
        # Common array format:
        # timestamp, open, high, low, close, volume
        df = pd.DataFrame(
            rows,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )

    required = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    if not all(col in df.columns for col in required):
        raise ValueError(
            f"Missing candle columns. Received: {list(df.columns)}"
        )

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)

    df = df.sort_values("timestamp")
    df = df.drop_duplicates(subset=["timestamp"])
    df = df.reset_index(drop=True)

    if len(df) < 110:
        raise ValueError(
            f"Insufficient candles: {len(df)}"
        )

    return df


def fetch_candles(symbol, timeframe, limit=250):
    if not CANDLE_URL:
        raise RuntimeError(
            "LBANK_FUTURES_CANDLE_URL is missing"
        )

    response = session.get(
        CANDLE_URL,
        params={
            "symbol": symbol,
            "interval": timeframe,
            "limit": limit,
        },
        timeout=20,
    )

    response.raise_for_status()

    return parse_candles(response.json())


def add_indicators(df):
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema20"] = close.ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = close.ewm(
        span=50, adjust=False
    ).mean()

    df["ema100"] = close.ewm(
        span=100, adjust=False
    ).mean()

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(
        span=12, adjust=False
    ).mean()

    ema26 = close.ewm(
        span=26, adjust=False
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9, adjust=False
    ).mean()

    df["macd_hist"] = (
        df["macd"] - df["macd_signal"]
    )

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = true_range.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(20).mean()

    return df.dropna().reset_index(drop=True)


def get_trend(df):
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


def get_structure(df):
    previous = df.iloc[-21:-1]
    last = df.iloc[-1]

    resistance = previous["high"].max()
    support = previous["low"].min()

    if last["close"] > resistance:
        return "BULLISH_BREAKOUT", support, resistance

    if last["close"] < support:
        return "BEARISH_BREAKDOWN", support, resistance

    return "RANGE", support, resistance


def calculate_trade(direction, entry, atr, df):
    if not np.isfinite(atr) or atr <= 0:
        return None

    recent = df.iloc[-10:]

    if direction == "LONG":
        swing_low = float(recent["low"].min())
        sl = min(swing_low, entry - 1.2 * atr)

        risk = entry - sl

        if risk <= 0:
            return None

        tp = entry + risk * MIN_RR

    else:
        swing_high = float(recent["high"].max())
        sl = max(swing_high, entry + 1.2 * atr)

        risk = sl - entry

        if risk <= 0:
            return None

        tp = entry - risk * MIN_RR

    if tp <= 0:
        return None

    rr = abs(tp - entry) / risk

    if rr < MIN_RR:
        return None

    return {
        "entry": round(entry, 10),
        "stop_loss": round(sl, 10),
        "take_profit": round(tp, 10),
        "rr": round(rr, 2),
    }


def analyze_symbol(symbol):
    data = {}

    for timeframe in TIMEFRAMES:
        raw = fetch_candles(symbol, timeframe)
        data[timeframe] = add_indicators(raw)
        time.sleep(0.15)

    h4 = get_trend(data["4h"])
    h1 = get_trend(data["1h"])

    if h4 == "NEUTRAL" or h4 != h1:
        return None, "HTF_TREND_MISMATCH"

    direction = h4

    m15 = data["15m"].iloc[-1]
    m5 = data["5m"]
    m1 = data["1m"].iloc[-1]

    structure, support, resistance = get_structure(m5)

    volume_ratio = (
        float(m1["volume"]) / float(m1["volume_avg"])
        if m1["volume_avg"] > 0 else 0
    )

    if direction == "LONG":

        if not (
            m15["close"] > m15["ema50"]
            and m15["macd_hist"] > 0
            and 50 < m15["rsi"] < 72
        ):
            return None, "15M_FILTER_FAILED"

        if structure != "BULLISH_BREAKOUT":
            return None, "NO_BULLISH_BREAKOUT"

        if not (
            m5.iloc[-1]["close"] > m5.iloc[-1]["ema20"]
            and m1["close"] > m1["ema20"]
            and m1["macd_hist"] > 0
        ):
            return None, "ENTRY_CONFIRMATION_FAILED"

    else:

        if not (
            m15["close"] < m15["ema50"]
            and m15["macd_hist"] < 0
            and 28 < m15["rsi"] < 50
        ):
            return None, "15M_FILTER_FAILED"

        if structure != "BEARISH_BREAKDOWN":
            return None, "NO_BEARISH_BREAKDOWN"

        if not (
            m5.iloc[-1]["close"] < m5.iloc[-1]["ema20"]
            and m1["close"] < m1["ema20"]
            and m1["macd_hist"] < 0
        ):
            return None, "ENTRY_CONFIRMATION_FAILED"

    if volume_ratio < 1.2:
        return None, "LOW_VOLUME"

    entry = float(m1["close"])
    atr = float(m1["atr"])

    trade = calculate_trade(
        direction,
        entry,
        atr,
        m5
    )

    if trade is None:
        return None, "INVALID_RISK_REWARD"

    signal = {
        "symbol": symbol,
        "direction": direction,
        **trade,
        "trend_4h": h4,
        "trend_1h": h1,
        "rsi_15m": round(float(m15["rsi"]), 2),
        "macd_15m": round(float(m15["macd_hist"]), 8),
        "volume_ratio": round(volume_ratio, 2),
        "structure_5m": structure,
        "support_5m": round(float(support), 10),
        "resistance_5m": round(float(resistance), 10),
        "source": "LBank Futures",
    }

    return signal, "VALID_SIGNAL"


def save_signals(signals):
    os.makedirs("data", exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            signals,
            f,
            ensure_ascii=False,
            indent=2
        )


def main():
    print("=" * 55)
    print("LBANK FUTURES MULTI-TIMEFRAME SCANNER")
    print("=" * 55)

    if not CANDLE_URL:
        raise RuntimeError(
            "No verified Futures candle endpoint configured. "
            "Scanner stopped safely."
        )

    symbols = load_symbols()

    print(f"Symbols loaded: {len(symbols)}")

    signals = []
    errors = 0
    rejected = 0
    checked = 0
    reasons = {}

    for item in symbols:
        symbol = item["symbol"]
        checked += 1

        try:
            signal, reason = analyze_symbol(symbol)

            reasons[reason] = reasons.get(reason, 0) + 1

            if signal:
                signals.append(signal)
                print(
                    f"SIGNAL: {symbol} "
                    f"{signal['direction']} "
                    f"RR={signal['rr']}"
                )
            else:
                rejected += 1
                print(f"REJECTED: {symbol} | {reason}")

        except Exception as exc:
            errors += 1
            print(f"ERROR: {symbol} | {exc}")

    signals.sort(
        key=lambda x: (
            x["rr"],
            x["volume_ratio"]
        ),
        reverse=True
    )

    signals = signals[:MAX_SIGNALS]

    save_signals(signals)

    print("\n" + "=" * 55)
    print("SCAN SUMMARY")
    print("=" * 55)

    print(f"CHECKED: {checked}")
    print(f"REJECTED: {rejected}")
    print(f"ERRORS: {errors}")
    print(f"VALID SIGNALS: {len(signals)}")

    print("\nREJECTION REASONS:")

    for reason, count in sorted(reasons.items()):
        print(f"{reason}: {count}")

    if not signals:
        print("\nNO SETUP - DO NOT ENTER")

    print("=" * 55)


if __name__ == "__main__":
    main()
