from typing import List, Optional, Tuple
import numpy as np
import pandas as pd

from .models import Candle


def candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms"])
    df = pd.DataFrame([c.to_dict() for c in candles])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_indicator_snapshot(candles: List[Candle]) -> Optional[dict]:
    df = candles_to_df(candles)
    if len(df) < 205:
        return None

    close = df["close"]
    vol = df["volume"]

    e20 = ema(close, 20)
    e50 = ema(close, 50)
    e200 = ema(close, 200)
    r14 = rsi(close, 14)
    macd_line, sig_line, hist = macd(close, 12, 26, 9)
    a14 = atr(df, 14)
    vol_sma20 = vol.rolling(20).mean()

    last_close = float(close.iloc[-1])
    last_e20 = float(e20.iloc[-1])
    last_e50 = float(e50.iloc[-1])
    last_e200 = float(e200.iloc[-1])
    last_rsi = float(r14.iloc[-1])
    prev_rsi = float(r14.iloc[-2])
    last_macd = float(macd_line.iloc[-1])
    last_sig = float(sig_line.iloc[-1])
    last_hist = float(hist.iloc[-1])
    prev_hist = float(hist.iloc[-2])
    last_atr = float(a14.iloc[-1])
    last_vol = float(vol.iloc[-1])
    last_vol_sma20 = float(vol_sma20.iloc[-1]) if pd.notna(vol_sma20.iloc[-1]) and float(vol_sma20.iloc[-1]) > 0 else last_vol

    vol_ratio = last_vol / last_vol_sma20 if last_vol_sma20 > 0 else 1.0

    return {
        "close": last_close,
        "ema20": last_e20,
        "ema50": last_e50,
        "ema200": last_e200,
        "rsi": last_rsi,
        "prev_rsi": prev_rsi,
        "macd": last_macd,
        "macd_signal": last_sig,
        "macd_hist": last_hist,
        "prev_macd_hist": prev_hist,
        "atr": last_atr,
        "volume": last_vol,
        "vol_sma20": last_vol_sma20,
        "vol_ratio": vol_ratio,
    }
