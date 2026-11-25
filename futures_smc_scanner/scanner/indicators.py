
import math
from typing import Any, Dict, List, Optional

import pandas as pd

from .models import Candle


def candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    columns = [
        "open_time_ms", "open", "high",
        "low", "close", "volume", "close_time_ms"
    ]

    if not candles:
        return pd.DataFrame(columns=columns)

    rows = [c.to_dict() for c in candles]
    df = pd.DataFrame(rows)

    required = [
        "open_time_ms", "open", "high",
        "low", "close", "volume", "close_time_ms"
    ]

    if any(col not in df.columns for col in required):
        return pd.DataFrame(columns=columns)

    df = df[required].copy()

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)
    df = df.sort_values("open_time_ms")
    df = df.drop_duplicates(subset=["open_time_ms"], keep="last")

    valid = (
        (df["open"] > 0)
        & (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["high"] >= df["low"])
    )

    df = df.loc[valid].reset_index(drop=True)

    return df


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(
        span=period,
        adjust=False,
        min_periods=period
    ).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + rs))

    result = result.mask(
        (avg_loss == 0) & (avg_gain > 0), 100.0
    )

    result = result.mask(
        (avg_gain == 0) & (avg_loss > 0), 0.0
    )

    result = result.mask(
        (avg_gain == 0) & (avg_loss == 0), 50.0
    )

    return result


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
):
    fast_ema = series.ewm(
        span=fast, adjust=False, min_periods=fast
    ).mean()

    slow_ema = series.ewm(
        span=slow, adjust=False, min_periods=slow
    ).mean()

    macd_line = fast_ema - slow_ema

    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()

    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def atr(
    df: pd.DataFrame,
    period: int = 14
) -> pd.Series:
    previous_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def compute_indicator_snapshot(
    df: pd.DataFrame
) -> Optional[Dict[str, Any]]:

    if df is None or len(df) < 220:
        return None

    required = ["open", "high", "low", "close", "volume"]

    if any(col not in df.columns for col in required):
        return None

    data = df.copy()

    for col in required:
        data[col] = pd.to_numeric(
            data[col], errors="coerce"
        )

    if data[required].isna().any().any():
        return None

    if not all(
        _finite(v)
        for v in data[required].iloc[-220:].to_numpy().flatten()
    ):
        return None

    if (data["close"] <= 0).any():
        return None

    if (data["volume"] < 0).any():
        return None

    close = data["close"]

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    rsi14 = rsi(close, 14)

    macd_line, signal_line, histogram = macd(close)

    atr14 = atr(data, 14)

    vol_sma20 = data["volume"].rolling(
        window=20,
        min_periods=20
    ).mean()

    values = {
        "close": close.iloc[-1],
        "ema20": ema20.iloc[-1],
        "ema50": ema50.iloc[-1],
        "ema200": ema200.iloc[-1],
        "rsi14": rsi14.iloc[-1],
        "macd": macd_line.iloc[-1],
        "macd_signal": signal_line.iloc[-1],
        "macd_hist": histogram.iloc[-1],
        "prev_macd_hist": histogram.iloc[-2],
        "atr14": atr14.iloc[-1],
        "volume": data["volume"].iloc[-1],
        "vol_sma20": vol_sma20.iloc[-1],
    }

    if not all(_finite(v) for v in values.values()):
        return None

    if values["vol_sma20"] <= 0:
        return None

    values["vol_ratio"] = (
        values["volume"] / values["vol_sma20"]
    )

    if not _finite(values["vol_ratio"]):
        return None

    if values["atr14"] <= 0:
        return None

    values["atr_pct"] = (
        values["atr14"] / values["close"]
    ) * 100.0

    return {
        key: float(value)
        for key, value in values.items()
    }
