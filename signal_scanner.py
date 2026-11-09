
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
signal_scanner.py
Smart Market Analyst V3
Corrected and safety-validated edition.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("signal_scanner")

# --------------------------------------------------
# GENERAL SETTINGS
# --------------------------------------------------

DEFAULT_SYMBOLS = (
    "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT,"
    "DOGE_USDT,ADA_USDT,AVAX_USDT,LINK_USDT,"
    "DOT_USDT,LTC_USDT,ATOM_USDT,NEAR_USDT,BCH_USDT"
)

def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 65.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
FEE_PCT = _env_float("FEE_PCT", 0.06)

MAX_STOP_PCT = 3.0
MIN_CANDLES = 100
MIN_RR = 1.5
WATCH_MIN = 60

STRUCT_CAP = 70
MSS_MAX_DELAY = 8
MSS_MIN_BODY = 0.8

# Safety: Futures OHLCV source must be verified.
MARKET_TYPE = os.getenv("MARKET_TYPE", "FUTURES").upper()

if MARKET_TYPE not in ("FUTURES", "SPOT"):
    raise ValueError("MARKET_TYPE must be FUTURES or SPOT")


TF_MAP: Dict[str, Tuple[str, int]] = {
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

TREND_FA = {
    "UP": "صعودی",
    "DOWN": "نزولی",
    "RANGE": "رنج",
}

TREND_ARROW = {
    "UP": "↑",
    "DOWN": "↓",
    "RANGE": "↔",
}

DECISION_FA = {
    "ENTER": "ورود",
    "CONDITIONAL": "شرطی (نیازمند تأیید)",
    "NO_ENTRY": "ورود نکن",
}


# --------------------------------------------------
# EXCEPTIONS
# --------------------------------------------------

class ScanError(Exception):
    """Recoverable error for a single symbol or request."""


class DataSourceError(ScanError):
    """Market data source is unavailable or not verified."""


# --------------------------------------------------
# CANDLE DATA STRUCTURE
# --------------------------------------------------

class Candles:

    def __init__(
        self,
        rows: List[
            Tuple[float, float, float, float, float, float]
        ],
    ):
        if not rows:
            raise DataSourceError("No candle data received")

        self.t = [float(r[0]) for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.v = [float(r[5]) for r in rows]

        self._validate()

    def _validate(self) -> None:

        n = len(self.c)

        if not all(
            len(x) == n
            for x in (
                self.t,
                self.o,
                self.h,
                self.l,
                self.v,
            )
        ):
            raise DataSourceError("Candle arrays have different lengths")

        for i in range(n):

            values = (
                self.o[i],
                self.h[i],
                self.l[i],
                self.c[i],
                self.v[i],
            )

            if not all(math.isfinite(x) for x in values):
                raise DataSourceError("Non-finite candle value")

            if min(
                self.o[i],
                self.h[i],
                self.l[i],
                self.c[i],
            ) <= 0:
                raise DataSourceError("Invalid candle price")

            if self.h[i] < self.l[i]:
                raise DataSourceError("Invalid candle high/low")

            if self.v[i] < 0:
                raise DataSourceError("Negative candle volume")

            if self.h[i] < max(self.o[i], self.c[i]):
                raise DataSourceError("Candle high is invalid")

            if self.l[i] > min(self.o[i], self.c[i]):
                raise DataSourceError("Candle low is invalid")

            if i > 0 and self.t[i] <= self.t[i - 1]:
                raise DataSourceError("Candle timestamps are not increasing")

        if n < MIN_CANDLES:
            raise DataSourceError(
                "Insufficient candles: %d (required %d)"
                % (n, MIN_CANDLES)
            )

    def __len__(self) -> int:
        return len(self.c)

# --------------------------------------------------------------------------
# PART 2/5: NETWORK, DATA SOURCE & MARKET DATA
# --------------------------------------------------------------------------

_last_req = 0.0

LBANK_BASE = os.getenv(
    "LBANK_BASE_URL",
    "https://api.lbkex.com"
).rstrip("/")


def _throttle() -> None:
    global _last_req

    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)

    if wait > 0:
        time.sleep(wait)

    _last_req = time.monotonic()


def http_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    retries: int = 4,
    timeout: float = 15.0,
) -> Any:

    if params:
        separator = "&" if "?" in url else "?"
        url += separator + urllib.parse.urlencode(params)

    data = None

    headers = {
        "User-Agent": "signal-scanner/3.1",
        "Accept": "application/json",
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_err = "unknown"

    for attempt in range(1, retries + 1):

        _throttle()

        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers=headers,
            )

            with urllib.request.urlopen(
                req,
                timeout=timeout,
            ) as resp:

                raw = resp.read().decode(
                    "utf-8",
                    "replace",
                )

            return json.loads(raw)

        except urllib.error.HTTPError as e:

            last_err = "HTTP %s" % e.code

            if e.code == 429 or e.code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue

            raise ScanError(
                "Network HTTP Error: %s" % last_err
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as e:

            last_err = str(
                getattr(e, "reason", None) or e
            )

            time.sleep(min(2 ** attempt, 20))

        except (json.JSONDecodeError, ValueError):

            last_err = "Invalid JSON response"

            time.sleep(min(2 ** attempt, 20))

    raise ScanError(
        "Request failed after %d attempts: %s"
        % (retries, last_err)
    )


# --------------------------------------------------------------------------
# DATA SOURCE VALIDATION
# --------------------------------------------------------------------------

def futures_data_source_verified() -> bool:
    """
    Futures OHLCV must not be inferred from Spot data
    or from periodic market snapshots.

    Until an official Futures candle source is verified,
    the scanner refuses to generate Futures signals.
    """

    return (
        os.getenv("FUTURES_OHLCV_VERIFIED", "false").lower()
        == "true"
        and bool(os.getenv("FUTURES_KLINE_URL", "").strip())
    )


def _validate_symbol(symbol: str) -> str:

    symbol = str(symbol).upper().strip()

    if not re.fullmatch(r"[A-Z0-9]+_[A-Z0-9]+", symbol):
        raise ScanError(
            "Invalid symbol format: %s" % symbol
        )

    return symbol


# --------------------------------------------------------------------------
# KLINE PARSING
# --------------------------------------------------------------------------

def _parse_kline_rows(
    rows_raw: Any,
    tf: str,
) -> Candles:

    if not isinstance(rows_raw, list):
        raise ScanError("Invalid kline data array")

    if tf not in TF_MAP:
        raise ScanError("Unsupported timeframe: %s" % tf)

    _, sec = TF_MAP[tf]

    parsed: Dict[
        int,
        Tuple[float, float, float, float, float, float]
    ] = {}

    now = time.time()

    for r in rows_raw:

        try:
            if isinstance(r, dict):
                ts = int(float(
                    r.get("timestamp", r.get("time"))
                ))

                o = float(r.get("open"))
                h = float(r.get("high"))
                l = float(r.get("low"))
                c = float(r.get("close"))
                v = float(r.get("volume"))

            else:
                ts = int(float(r[0]))

                o, h, l, c, v = (
                    float(x) for x in r[1:6]
                )

            # Convert milliseconds to seconds.
            if ts > 10 ** 11:
                ts //= 1000

            values = (o, h, l, c, v)

            if not all(math.isfinite(x) for x in values):
                continue

            if min(o, h, l, c) <= 0:
                continue

            if h < max(o, c) or l > min(o, c):
                continue

            if h < l or v < 0:
                continue

            # Ignore future candles.
            if ts > now:
                continue

            # Ignore candles that have not closed.
            if ts + sec > now:
                continue

            parsed[ts] = (
                float(ts),
                o,
                h,
                l,
                c,
                v,
            )

        except (
            TypeError,
            ValueError,
            IndexError,
            OverflowError,
        ):
            continue

    rows = [
        parsed[k]
        for k in sorted(parsed)
    ]

    if len(rows) < MIN_CANDLES:
        raise ScanError(
            "Insufficient closed candles: %d (required %d)"
            % (len(rows), MIN_CANDLES)
        )

    return Candles(rows)


# --------------------------------------------------------------------------
# FETCH KLINES
# --------------------------------------------------------------------------

def fetch_klines(
    symbol: str,
    tf: str,
    size: int = 300,
) -> Candles:

    symbol = _validate_symbol(symbol)

    if tf not in TF_MAP:
        raise ScanError(
            "Unsupported timeframe: %s" % tf
        )

    if size < MIN_CANDLES or size > 1000:
        raise ScanError("Invalid candle request size")

    if MARKET_TYPE == "FUTURES":

        if not futures_data_source_verified():
            raise DataSourceError(
                "Futures OHLCV source is not verified. "
                "Spot candles are prohibited for Futures analysis."
            )

        url = os.getenv(
            "FUTURES_KLINE_URL",
            "",
        ).strip()

        # No guessed endpoint or automatic Spot fallback.
        if not url:
            raise DataSourceError(
                "Futures kline URL is missing"
            )

        raise DataSourceError(
            "Futures adapter requires a verified LBank "
            "request format and response mapping. "
            "No unverified endpoint will be called."
        )

    # Explicit SPOT mode only.
    typ, sec = TF_MAP[tf]

    now = int(time.time())
    start = now - size * sec

    data = http_json(
        LBANK_BASE + "/v2/kline.do",
        {
            "symbol": symbol.lower(),
            "size": size,
            "type": typ,
            "time": start,
        },
    )

    if (
        not isinstance(data, dict)
        or str(data.get("result", "")).lower() != "true"
    ):
        raise ScanError(
            "Invalid Spot kline response for %s [%s]"
            % (symbol, tf)
        )

    return _parse_kline_rows(
        data.get("data"),
        tf,
    )


# --------------------------------------------------------------------------
# FETCH PRICE
# --------------------------------------------------------------------------

def fetch_price(symbol: str) -> Optional[float]:

    symbol = _validate_symbol(symbol)

    if MARKET_TYPE == "FUTURES":
        raise DataSourceError(
            "Futures price adapter is not verified"
        )

    try:

        data = http_json(
            LBANK_BASE + "/v2/ticker/24hr.do",
            {"symbol": symbol.lower()},
        )

        items = (
            data.get("data")
            if isinstance(data, dict)
            else None
        )

        if not isinstance(items, list) or not items:
            return None

        ticker = items[0].get("ticker", {})

        price = float(ticker.get("latest"))

        if math.isfinite(price) and price > 0:
            return price

    except (
        ScanError,
        TypeError,
        ValueError,
        KeyError,
    ):
        log.warning(
            "Unable to fetch Spot price for %s",
            symbol,
        )

    return None


# --------------------------------------------------------------------------
# FETCH SPOT PAIRS
# --------------------------------------------------------------------------

def fetch_pairs() -> Optional[set]:

    if MARKET_TYPE != "SPOT":
        return None

    try:

        data = http_json(
            LBANK_BASE + "/v2/currencyPairs.do"
        )

    except ScanError:
        return None

    items = (
        data.get("data")
        if isinstance(data, dict)
        else None
    )

    if not isinstance(items, list):
        return None

    pairs = {
        str(x).lower()
        for x in items
        if isinstance(x, str)
    }

    return pairs if pairs else None

# --------------------------------------------------------------------------
# PART 3/5: INDICATORS, MARKET STRUCTURE & TRADE VALIDATOR
# --------------------------------------------------------------------------

def ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []

    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]

    for value in values[1:]:
        result.append(
            alpha * value + (1.0 - alpha) * result[-1]
        )

    return result


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

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
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, float]:

    if len(values) < slow + signal:
        return {
            "macd": 0.0,
            "signal": 0.0,
            "histogram": 0.0,
        }

    fast_line = ema(values, fast)
    slow_line = ema(values, slow)

    line = [
        a - b
        for a, b in zip(fast_line, slow_line)
    ]

    signal_line = ema(line, signal)

    return {
        "macd": line[-1],
        "signal": signal_line[-1],
        "histogram": line[-1] - signal_line[-1],
    }


def atr(
    h: List[float],
    l: List[float],
    c: List[float],
    n: int = 14,
) -> List[float]:

    if len(c) < n or len(h) != len(c) or len(l) != len(c):
        raise ScanError("Insufficient data for ATR")

    trs = [h[0] - l[0]]

    for i in range(1, len(c)):
        trs.append(
            max(
                h[i] - l[i],
                abs(h[i] - c[i - 1]),
                abs(l[i] - c[i - 1]),
            )
        )

    out = []
    current = sum(trs[:n]) / n

    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            out.append(current)
        else:
            current = (
                current * (n - 1) + tr
            ) / n

            out.append(current)

    return out


def volume_ratio(v: List[float]) -> float:
    if len(v) < 24:
        return 1.0

    recent = sum(v[-3:]) / 3.0
    base = sum(v[-23:-3]) / 20.0

    if base <= 0:
        return 1.0

    return recent / base


# --------------------------------------------------------------------------
# MARKET STRUCTURE
# --------------------------------------------------------------------------

def find_swings(
    h: List[float],
    l: List[float],
    n: int = 3,
) -> Tuple[
    List[Tuple[int, float]],
    List[Tuple[int, float]],
]:

    highs = []
    lows = []

    for i in range(n, len(h) - n):

        if (
            all(h[i] > h[i - k] for k in range(1, n + 1))
            and
            all(h[i] >= h[i + k] for k in range(1, n + 1))
        ):
            highs.append((i, h[i]))

        if (
            all(l[i] < l[i - k] for k in range(1, n + 1))
            and
            all(l[i] <= l[i + k] for k in range(1, n + 1))
        ):
            lows.append((i, l[i]))

    return highs, lows


def structure_dir(
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
) -> int:

    if len(highs) < 2 or len(lows) < 2:
        return 0

    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]

    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]

    if hh and hl:
        return 1

    if lh and ll:
        return -1

    return 0


def structure_trend(
    c: List[float],
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
) -> str:

    if not c or not highs or not lows:
        return "RANGE"

    st = structure_dir(highs, lows)
    last = c[-1]

    if st == 1:
        return "RANGE" if last < lows[-1][1] else "UP"

    if st == -1:
        return "RANGE" if last > highs[-1][1] else "DOWN"

    if last > highs[-1][1]:
        return "UP"

    if last < lows[-1][1]:
        return "DOWN"

    return "RANGE"


def build_tf(
    cs: Candles,
    name: str,
) -> Dict[str, Any]:

    c, h, l = cs.c, cs.h, cs.l

    at = atr(h, l, c)

    highs, lows = find_swings(
        h,
        l,
        2 if name == "4h" else 3,
    )

    st = structure_dir(highs, lows)

    recent_atr = at[-100:]

    atr_rank = (
        sum(1 for x in recent_atr if x <= at[-1])
        / float(len(recent_atr))
    )

    trend = structure_trend(c, highs, lows)

    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)

    m = macd(c)

    return {
        "name": name,
        "cs": cs,
        "close": c[-1],
        "atr": max(at[-1], c[-1] * 1e-6),
        "vol_ratio": volume_ratio(cs.v),
        "highs": highs,
        "lows": lows,
        "struct": st,
        "trend": trend,
        "atr_rank": atr_rank,
        "atr_list": at,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi(c),
        "macd": m,
    }


# --------------------------------------------------------------------------
# SUPPORT & RESISTANCE
# --------------------------------------------------------------------------

def liquidity_map(
    d: Dict[str, Any],
    price: float,
) -> Dict[str, Any]:

    highs = [
        p[1] for p in d["highs"]
        if p[1] > price
    ]

    lows = [
        p[1] for p in d["lows"]
        if p[1] < price
    ]

    resistance = min(highs) if highs else None
    support = max(lows) if lows else None

    return {
        "support": support,
        "resistance": resistance,
    }


# --------------------------------------------------------------------------
# STRICT TRADE PLAN VALIDATOR
# --------------------------------------------------------------------------

def validate_and_correct_trade_plan(
    direction: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    recovery_lvl: Optional[float],
    swings_h: List[Tuple[int, float]],
    swings_l: List[Tuple[int, float]],
) -> Dict[str, Any]:

    errors = []

    values = (entry, sl, tp1, tp2)

    if not all(
        math.isfinite(x) and x > 0
        for x in values
    ):
        return {
            "Entry": entry,
            "SL": sl,
            "TP1": tp1,
            "TP2": tp2,
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": [
                "قیمت‌های طرح معامله نامعتبر هستند"
            ],
        }

    if direction not in ("LONG", "SHORT"):
        errors.append("جهت معامله نامعتبر است")

    if recovery_lvl is not None:
        if not math.isfinite(recovery_lvl) or recovery_lvl <= 0:
            errors.append("سطح بازیابی نامعتبر است")
        elif direction == "LONG" and entry < recovery_lvl:
            entry = recovery_lvl
        elif direction == "SHORT" and entry > recovery_lvl:
            entry = recovery_lvl

    if direction == "LONG":

        if sl >= entry:
            errors.append("حد ضرر LONG باید پایین‌تر از ورود باشد")

        if tp1 <= entry or tp2 <= entry:
            errors.append("تارگت LONG باید بالاتر از ورود باشد")

        if tp2 < tp1:
            errors.append("ترتیب TP1 و TP2 در LONG اشتباه است")

    elif direction == "SHORT":

        if sl <= entry:
            errors.append("حد ضرر SHORT باید بالاتر از ورود باشد")

        if tp1 >= entry or tp2 >= entry:
            errors.append("تارگت SHORT باید پایین‌تر از ورود باشد")

        if tp2 > tp1:
            errors.append("ترتیب TP1 و TP2 در SHORT اشتباه است")

    if errors:
        return {
            "Entry": round(entry, 8),
            "SL": round(sl, 8),
            "TP1": round(tp1, 8),
            "TP2": round(tp2, 8),
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": errors,
        }

    risk = abs(entry - sl)

    if risk <= 0:
        errors.append("ریسک معامله صفر است")

    if errors:
        return {
            "Entry": round(entry, 8),
            "SL": round(sl, 8),
            "TP1": round(tp1, 8),
            "TP2": round(tp2, 8),
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": errors,
        }

    risk_pct = risk / entry * 100.0

    if risk_pct > MAX_STOP_PCT:
        errors.append(
            "حد ضرر بیش از %.2f%% است (%.2f%%)"
            % (MAX_STOP_PCT, risk_pct)
        )

    reward1 = abs(tp1 - entry)
    reward2 = abs(tp2 - entry)

    rr1 = round(reward1 / risk, 2)
    rr2 = round(reward2 / risk, 2)

    if rr1 <= 0:
        errors.append("نسبت ریسک به بازده TP1 نامعتبر است")

    if rr2 < MIN_RR:
        errors.append(
            "نسبت ریسک به بازده TP2 کمتر از %.2f است"
            % MIN_RR
        )

    valid = not errors

    return {
        "Entry": round(entry, 8),
        "SL": round(sl, 8),
        "TP1": round(tp1, 8),
        "TP2": round(tp2, 8),
        "Net_RR1": rr1,
        "Net_RR2": rr2,
        "Risk_Pct": round(risk_pct, 4),
        "Valid": valid,
        "Validator_Errors": errors,
    }


# --------------------------------------------------------------------------
# STRUCTURAL ENTRY, SL & TARGETS
# --------------------------------------------------------------------------

def calculate_levels_and_targets(
    cs: Candles,
    direction: str = "LONG",
    entry_price: float = 0,
    swings_h=None,
    swings_l=None,
) -> Dict[str, Any]:

    if entry_price <= 0:
        raise ScanError("Invalid entry price")

    if swings_h is None or swings_l is None:
        swings_h, swings_l = find_swings(
            cs.h,
            cs.l,
            n=3,
        )

    lookback = 60

    highs_raw = cs.h[-lookback:]
    lows_raw = cs.l[-lookback:]

    structural_highs = [
        p[1] for p in swings_h
        if p[1] > entry_price
    ]

    structural_lows = [
        p[1] for p in swings_l
        if p[1] < entry_price
    ]

    structural_low = (
        min(lows_raw) if lows_raw
        else entry_price * 0.99
    )

    structural_high = (
        max(highs_raw) if highs_raw
        else entry_price * 1.01
    )

    if direction == "LONG":

        valid_lows = [
            p[1] for p in swings_l
            if p[1] < entry_price
        ]

        sl_base = (
            max(valid_lows)
            if valid_lows
            else structural_low
        )

        sl = sl_base * 0.995

        risk = entry_price - sl

        if risk <= 0:
            raise ScanError("Invalid LONG structural stop")

        tp1 = (
            min(structural_highs)
            if structural_highs
            else entry_price + risk * 1.5
        )

        tp2 = (
            max(structural_highs)
            if structural_highs
            else entry_price + risk * 2.5
        )

        if tp2 <= tp1:
            tp2 = entry_price + risk * 2.5

    elif direction == "SHORT":

        valid_highs = [
            p[1] for p in swings_h
            if p[1] > entry_price
        ]

        sl_base = (
            min(valid_highs)
            if valid_highs
            else structural_high
        )

        sl = sl_base * 1.005

        risk = sl - entry_price

        if risk <= 0:
            raise ScanError("Invalid SHORT structural stop")

        tp1 = (
            max(structural_lows)
            if structural_lows
            else entry_price - risk * 1.5
        )

        tp2 = (
            min(structural_lows)
            if structural_lows
            else entry_price - risk * 2.5
        )

        if tp2 >= tp1:
            tp2 = entry_price - risk * 2.5

    else:
        raise ScanError("Unknown trade direction")

    return validate_and_correct_trade_plan(
        direction,
        entry_price,
        sl,
        tp1,
        tp2,
        None,
        swings_h,
        swings_l,
    )

# --------------------------------------------------------------------------
# PART 3/5: INDICATORS, MARKET STRUCTURE & TRADE VALIDATOR
# --------------------------------------------------------------------------

def ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []

    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]

    for value in values[1:]:
        result.append(
            alpha * value + (1.0 - alpha) * result[-1]
        )

    return result


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

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
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, float]:

    if len(values) < slow + signal:
        return {
            "macd": 0.0,
            "signal": 0.0,
            "histogram": 0.0,
        }

    fast_line = ema(values, fast)
    slow_line = ema(values, slow)

    line = [
        a - b
        for a, b in zip(fast_line, slow_line)
    ]

    signal_line = ema(line, signal)

    return {
        "macd": line[-1],
        "signal": signal_line[-1],
        "histogram": line[-1] - signal_line[-1],
    }


def atr(
    h: List[float],
    l: List[float],
    c: List[float],
    n: int = 14,
) -> List[float]:

    if len(c) < n or len(h) != len(c) or len(l) != len(c):
        raise ScanError("Insufficient data for ATR")

    trs = [h[0] - l[0]]

    for i in range(1, len(c)):
        trs.append(
            max(
                h[i] - l[i],
                abs(h[i] - c[i - 1]),
                abs(l[i] - c[i - 1]),
            )
        )

    out = []
    current = sum(trs[:n]) / n

    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            out.append(current)
        else:
            current = (
                current * (n - 1) + tr
            ) / n

            out.append(current)

    return out


def volume_ratio(v: List[float]) -> float:
    if len(v) < 24:
        return 1.0

    recent = sum(v[-3:]) / 3.0
    base = sum(v[-23:-3]) / 20.0

    if base <= 0:
        return 1.0

    return recent / base


# --------------------------------------------------------------------------
# MARKET STRUCTURE
# --------------------------------------------------------------------------

def find_swings(
    h: List[float],
    l: List[float],
    n: int = 3,
) -> Tuple[
    List[Tuple[int, float]],
    List[Tuple[int, float]],
]:

    highs = []
    lows = []

    for i in range(n, len(h) - n):

        if (
            all(h[i] > h[i - k] for k in range(1, n + 1))
            and
            all(h[i] >= h[i + k] for k in range(1, n + 1))
        ):
            highs.append((i, h[i]))

        if (
            all(l[i] < l[i - k] for k in range(1, n + 1))
            and
            all(l[i] <= l[i + k] for k in range(1, n + 1))
        ):
            lows.append((i, l[i]))

    return highs, lows


def structure_dir(
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
) -> int:

    if len(highs) < 2 or len(lows) < 2:
        return 0

    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]

    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]

    if hh and hl:
        return 1

    if lh and ll:
        return -1

    return 0


def structure_trend(
    c: List[float],
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
) -> str:

    if not c or not highs or not lows:
        return "RANGE"

    st = structure_dir(highs, lows)
    last = c[-1]

    if st == 1:
        return "RANGE" if last < lows[-1][1] else "UP"

    if st == -1:
        return "RANGE" if last > highs[-1][1] else "DOWN"

    if last > highs[-1][1]:
        return "UP"

    if last < lows[-1][1]:
        return "DOWN"

    return "RANGE"


def build_tf(
    cs: Candles,
    name: str,
) -> Dict[str, Any]:

    c, h, l = cs.c, cs.h, cs.l

    at = atr(h, l, c)

    highs, lows = find_swings(
        h,
        l,
        2 if name == "4h" else 3,
    )

    st = structure_dir(highs, lows)

    recent_atr = at[-100:]

    atr_rank = (
        sum(1 for x in recent_atr if x <= at[-1])
        / float(len(recent_atr))
    )

    trend = structure_trend(c, highs, lows)

    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)

    m = macd(c)

    return {
        "name": name,
        "cs": cs,
        "close": c[-1],
        "atr": max(at[-1], c[-1] * 1e-6),
        "vol_ratio": volume_ratio(cs.v),
        "highs": highs,
        "lows": lows,
        "struct": st,
        "trend": trend,
        "atr_rank": atr_rank,
        "atr_list": at,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi(c),
        "macd": m,
    }


# --------------------------------------------------------------------------
# SUPPORT & RESISTANCE
# --------------------------------------------------------------------------

def liquidity_map(
    d: Dict[str, Any],
    price: float,
) -> Dict[str, Any]:

    highs = [
        p[1] for p in d["highs"]
        if p[1] > price
    ]

    lows = [
        p[1] for p in d["lows"]
        if p[1] < price
    ]

    resistance = min(highs) if highs else None
    support = max(lows) if lows else None

    return {
        "support": support,
        "resistance": resistance,
    }


# --------------------------------------------------------------------------
# STRICT TRADE PLAN VALIDATOR
# --------------------------------------------------------------------------

def validate_and_correct_trade_plan(
    direction: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    recovery_lvl: Optional[float],
    swings_h: List[Tuple[int, float]],
    swings_l: List[Tuple[int, float]],
) -> Dict[str, Any]:

    errors = []

    values = (entry, sl, tp1, tp2)

    if not all(
        math.isfinite(x) and x > 0
        for x in values
    ):
        return {
            "Entry": entry,
            "SL": sl,
            "TP1": tp1,
            "TP2": tp2,
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": [
                "قیمت‌های طرح معامله نامعتبر هستند"
            ],
        }

    if direction not in ("LONG", "SHORT"):
        errors.append("جهت معامله نامعتبر است")

    if recovery_lvl is not None:
        if not math.isfinite(recovery_lvl) or recovery_lvl <= 0:
            errors.append("سطح بازیابی نامعتبر است")
        elif direction == "LONG" and entry < recovery_lvl:
            entry = recovery_lvl
        elif direction == "SHORT" and entry > recovery_lvl:
            entry = recovery_lvl

    if direction == "LONG":

        if sl >= entry:
            errors.append("حد ضرر LONG باید پایین‌تر از ورود باشد")

        if tp1 <= entry or tp2 <= entry:
            errors.append("تارگت LONG باید بالاتر از ورود باشد")

        if tp2 < tp1:
            errors.append("ترتیب TP1 و TP2 در LONG اشتباه است")

    elif direction == "SHORT":

        if sl <= entry:
            errors.append("حد ضرر SHORT باید بالاتر از ورود باشد")

        if tp1 >= entry or tp2 >= entry:
            errors.append("تارگت SHORT باید پایین‌تر از ورود باشد")

        if tp2 > tp1:
            errors.append("ترتیب TP1 و TP2 در SHORT اشتباه است")

    if errors:
        return {
            "Entry": round(entry, 8),
            "SL": round(sl, 8),
            "TP1": round(tp1, 8),
            "TP2": round(tp2, 8),
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": errors,
        }

    risk = abs(entry - sl)

    if risk <= 0:
        errors.append("ریسک معامله صفر است")

    if errors:
        return {
            "Entry": round(entry, 8),
            "SL": round(sl, 8),
            "TP1": round(tp1, 8),
            "TP2": round(tp2, 8),
            "Net_RR1": 0.0,
            "Net_RR2": 0.0,
            "Valid": False,
            "Validator_Errors": errors,
        }

    risk_pct = risk / entry * 100.0

    if risk_pct > MAX_STOP_PCT:
        errors.append(
            "حد ضرر بیش از %.2f%% است (%.2f%%)"
            % (MAX_STOP_PCT, risk_pct)
        )

    reward1 = abs(tp1 - entry)
    reward2 = abs(tp2 - entry)

    rr1 = round(reward1 / risk, 2)
    rr2 = round(reward2 / risk, 2)

    if rr1 <= 0:
        errors.append("نسبت ریسک به بازده TP1 نامعتبر است")

    if rr2 < MIN_RR:
        errors.append(
            "نسبت ریسک به بازده TP2 کمتر از %.2f است"
            % MIN_RR
        )

    valid = not errors

    return {
        "Entry": round(entry, 8),
        "SL": round(sl, 8),
        "TP1": round(tp1, 8),
        "TP2": round(tp2, 8),
        "Net_RR1": rr1,
        "Net_RR2": rr2,
        "Risk_Pct": round(risk_pct, 4),
        "Valid": valid,
        "Validator_Errors": errors,
    }


# --------------------------------------------------------------------------
# STRUCTURAL ENTRY, SL & TARGETS
# --------------------------------------------------------------------------

def calculate_levels_and_targets(
    cs: Candles,
    direction: str = "LONG",
    entry_price: float = 0,
    swings_h=None,
    swings_l=None,
) -> Dict[str, Any]:

    if entry_price <= 0:
        raise ScanError("Invalid entry price")

    if swings_h is None or swings_l is None:
        swings_h, swings_l = find_swings(
            cs.h,
            cs.l,
            n=3,
        )

    lookback = 60

    highs_raw = cs.h[-lookback:]
    lows_raw = cs.l[-lookback:]

    structural_highs = [
        p[1] for p in swings_h
        if p[1] > entry_price
    ]

    structural_lows = [
        p[1] for p in swings_l
        if p[1] < entry_price
    ]

    structural_low = (
        min(lows_raw) if lows_raw
        else entry_price * 0.99
    )

    structural_high = (
        max(highs_raw) if highs_raw
        else entry_price * 1.01
    )

    if direction == "LONG":

        valid_lows = [
            p[1] for p in swings_l
            if p[1] < entry_price
        ]

        sl_base = (
            max(valid_lows)
            if valid_lows
            else structural_low
        )

        sl = sl_base * 0.995

        risk = entry_price - sl

        if risk <= 0:
            raise ScanError("Invalid LONG structural stop")

        tp1 = (
            min(structural_highs)
            if structural_highs
            else entry_price + risk * 1.5
        )

        tp2 = (
            max(structural_highs)
            if structural_highs
            else entry_price + risk * 2.5
        )

        if tp2 <= tp1:
            tp2 = entry_price + risk * 2.5

    elif direction == "SHORT":

        valid_highs = [
            p[1] for p in swings_h
            if p[1] > entry_price
        ]

        sl_base = (
            min(valid_highs)
            if valid_highs
            else structural_high
        )

        sl = sl_base * 1.005

        risk = sl - entry_price

        if risk <= 0:
            raise ScanError("Invalid SHORT structural stop")

        tp1 = (
            max(structural_lows)
            if structural_lows
            else entry_price - risk * 1.5
        )

        tp2 = (
            min(structural_lows)
            if structural_lows
            else entry_price - risk * 2.5
        )

        if tp2 >= tp1:
            tp2 = entry_price - risk * 2.5

    else:
        raise ScanError("Unknown trade direction")

    return validate_and_correct_trade_plan(
        direction,
        entry_price,
        sl,
        tp1,
        tp2,
        None,
        swings_h,
        swings_l,
    )

def make_watch(symbol: str, price: float, long_result: dict, short_result: dict):
    """
    Build a watch-only result when neither direction qualifies for a trade.
    """
    best = max(
        (long_result, short_result),
        key=lambda x: float(x.get("score", 0) or 0),
    )

    return {
        "symbol": symbol,
        "price": price,
        "signal": None,
        "watch": {
            "side": best.get("side"),
            "score": best.get("score", 0),
            "reasons": best.get("reasons", []),
            "blockers": best.get("blockers", []),
        },
        "long": long_result,
        "short": short_result,
        "decision": "NO TRADE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _extract_price(raw):
    """
    Extract a usable price from common ticker response formats.
    """
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) and value > 0 else None

    if isinstance(raw, dict):
        for key in ("last", "lastPrice", "price", "close", "ticker"):
            value = raw.get(key)
            if isinstance(value, (int, float, str)):
                try:
                    number = float(value)
                    if math.isfinite(number) and number > 0:
                        return number
                except (TypeError, ValueError):
                    pass

        data = raw.get("data")
        if isinstance(data, dict):
            return _extract_price(data)
        if isinstance(data, list) and data:
            return _extract_price(data[0])

    if isinstance(raw, list) and raw:
        return _extract_price(raw[0])

    return None


def analyze_symbol(symbol: str):
    """
    Analyze one symbol across 4h, 1h, 15m and 5m.
    """
    symbol = _validate_symbol(symbol)

    raw_price = fetch_price(symbol)
    price = _extract_price(raw_price)

    if price is None:
        raise DataSourceError(f"قیمت معتبر برای {symbol} دریافت نشد")

    data = {}

    # Fetch all required timeframes.
    for tf, interval in TF_MAP.items():
        candles = fetch_klines(symbol, interval, limit=300)
        if not isinstance(candles, Candles):
            raise DataSourceError(
                f"داده کندل {tf} برای {symbol} معتبر نیست"
            )

        data[tf] = build_tf(candles)

    # Evaluate both directions independently.
    long_result = evaluate_side(data, "LONG", price)
    short_result = evaluate_side(data, "SHORT", price)

    qualified = []

    if long_result.get("decision") == "LONG" and long_result.get("plan"):
        qualified.append(long_result)

    if short_result.get("decision") == "SHORT" and short_result.get("plan"):
        qualified.append(short_result)

    # Never issue a signal if both directions qualify simultaneously.
    if len(qualified) != 1:
        return make_watch(symbol, price, long_result, short_result)

    signal = qualified[0]

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "watch": None,
        "long": long_result,
        "short": short_result,
        "decision": signal["decision"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def fmt_price(value):
    """
    Format prices without unnecessary trailing zeros.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"

    if not math.isfinite(value):
        return "-"

    if value >= 1000:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.5f}".rstrip("0").rstrip(".")
    if value >= 0.001:
        return f"{value:.7f}".rstrip("0").rstrip(".")

    return f"{value:.10f}".rstrip("0").rstrip(".")


def plan_fields(plan: dict):
    """
    Return normalized trade-plan fields for reporting.
    """
    if not isinstance(plan, dict):
        return {}

    return {
        "entry": plan.get("Entry"),
        "sl": plan.get("SL"),
        "tp1": plan.get("TP1"),
        "tp2": plan.get("TP2"),
        "rr": plan.get("Net_RR"),
        "risk_pct": plan.get("Risk_Pct"),
        "position_multiple": plan.get("Position_Risk_Multiple"),
        "leverage_cap": plan.get("Leverage_Cap"),
    }


def build_card(result: dict):
    """
    Build a readable report section for one symbol.
    """
    symbol = html.escape(str(result.get("symbol", "UNKNOWN")))
    price = fmt_price(result.get("price"))

    signal = result.get("signal")

    lines = [
        f"<b>{symbol}</b>",
        f"قیمت: <code>{price}</code>",
    ]

    if not signal:
        watch = result.get("watch") or {}
        side = watch.get("side", "-")
        score = watch.get("score", 0)

        lines.extend([
            "تصمیم: <b>NO TRADE</b>",
            f"جهت تحت نظر: {side}",
            f"امتیاز: {score}",
        ])

        blockers = watch.get("blockers") or []
        if blockers:
            lines.append("<b>دلایل عدم ورود:</b>")
            for item in blockers[:5]:
                lines.append(f"• {html.escape(str(item))}")

        return "\n".join(lines)

    plan = plan_fields(signal.get("plan") or {})

    lines.extend([
        f"سیگنال: <b>{signal.get('decision')}</b>",
        f"امتیاز: <b>{signal.get('score')}</b>",
        f"ورود: <code>{fmt_price(plan.get('entry'))}</code>",
        f"حد ضرر: <code>{fmt_price(plan.get('sl'))}</code>",
        f"تارگت ۱: <code>{fmt_price(plan.get('tp1'))}</code>",
        f"تارگت ۲: <code>{fmt_price(plan.get('tp2'))}</code>",
        f"RR خالص تقریبی: <b>{plan.get('rr', '-')}</b>",
        f"ریسک تخمینی موقعیت: {plan.get('risk_pct', '-')}%",
        f"ضریب اسمی موقعیت: {plan.get('position_multiple', '-')}",
    ])

    reasons = signal.get("reasons") or []
    if reasons:
        lines.append("<b>دلایل سیگنال:</b>")
        for item in reasons[:6]:
            lines.append(f"• {html.escape(str(item))}")

    return "\n".join(lines)


def format_brief(results: list):
    """
    Compact summary of scan results.
    """
    total = len(results)
    signals = [
        item for item in results
        if item.get("signal") and item["signal"].get("plan")
    ]

    long_count = sum(
        1 for item in signals
        if item["signal"].get("decision") == "LONG"
    )
    short_count = sum(
        1 for item in signals
        if item["signal"].get("decision") == "SHORT"
    )

    return (
        "گزارش خلاصه اسکن بازار\n"
        f"تعداد نمادها: {total}\n"
        f"سیگنال LONG: {long_count}\n"
        f"سیگنال SHORT: {short_count}\n"
        f"بدون ورود: {total - len(signals)}"
    )


def format_report(results: list):
    """
    Build the full Telegram-compatible HTML report.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = [
        "<b>SMART MARKET ANALYST V3</b>",
        f"Market: {MARKET_TYPE}",
        f"زمان گزارش: {now}",
        "",
        html.escape(format_brief(results)),
        "",
        "------------------------------",
    ]

    cards = []
    for result in results:
        cards.append(build_card(result))
        cards.append("------------------------------")

    footer = [
        "",
        "<b>مدیریت ریسک:</b>",
        f"ریسک پایه: {RISK_PCT}%",
        f"حداکثر اهرم مجاز در تنظیمات: {MAX_LEVERAGE}x",
        "اهرم بالا می‌تواند باعث لیکوییدشدن سریع سرمایه شود.",
        "این گزارش توصیه مالی قطعی نیست.",
    ]

    return "\n".join(header + cards + footer)


def _parse_symbols(raw_symbols):
    """
    Normalize comma-separated symbols.
    """
    if not raw_symbols:
        return list(DEFAULT_SYMBOLS)

    symbols = []
    for item in raw_symbols.split(","):
        item = item.strip().upper()
        if item and item not in symbols:
            symbols.append(item)

    return symbols


def main():
    parser = argparse.ArgumentParser(
        description="Smart Market Analyst V3"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="نمادها با کاما جدا شوند؛ مثال BTC_USDT,ETH_USDT",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="report.txt",
        help="مسیر فایل گزارش",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    symbols = _parse_symbols(args.symbols)

    if not symbols:
        logging.error("هیچ نمادی برای اسکن مشخص نشده است")
        return 1

    # Do not silently substitute Spot data for Futures.
    if MARKET_TYPE == "FUTURES":
        if not futures_data_source_verified():
            logging.error(
                "اسکن Futures متوقف شد: منبع OHLCV فیوچرز هنوز "
                "با endpoint و قالب پاسخ تأییدشده تنظیم نشده است."
            )
            return 1

        logging.error(
            "آداپتور واقعی Futures هنوز در fetch_klines و fetch_price "
            "پیاده‌سازی نشده است؛ برای جلوگیری از سیگنال جعلی، اسکن متوقف شد."
        )
        return 1

    # Spot mode only.
    try:
        available_pairs = fetch_pairs()
    except Exception as exc:
        logging.error("دریافت فهرست نمادها ناموفق بود: %s", exc)
        return 1

    if isinstance(available_pairs, dict):
        available_pairs = available_pairs.get("data", [])

    if not isinstance(available_pairs, (list, tuple, set)):
        logging.error("قالب فهرست نمادهای صرافی معتبر نیست")
        return 1

    normalized_pairs = set()
    for pair in available_pairs:
        if isinstance(pair, dict):
            pair = (
                pair.get("symbol")
                or pair.get("pair")
                or pair.get("currency_pair")
            )

        if pair:
            normalized_pairs.add(str(pair).upper())

    if not normalized_pairs:
        logging.error("فهرست نمادهای قابل معامله خالی است")
        return 1

    results = []

    for symbol in symbols:
        try:
            symbol = _validate_symbol(symbol)

            if symbol.upper() not in normalized_pairs:
                logging.warning(
                    "%s در فهرست نمادهای Spot صرافی پیدا نشد",
                    symbol,
                )
                continue

            logging.info("شروع تحلیل %s", symbol)
            result = analyze_symbol(symbol)
            results.append(result)

            logging.info(
                "%s | decision=%s",
                symbol,
                result.get("decision"),
            )

        except Exception as exc:
            logging.exception(
                "تحلیل %s با خطا مواجه شد: %s",
                symbol,
                exc,
            )

        time.sleep(max(0.0, REQUEST_INTERVAL))

    if not results:
        logging.error("هیچ نمادی با موفقیت تحلیل نشد")
        return 1

    report = format_report(results)

    try:
        output_path = os.path.abspath(args.output)
        output_dir = os.path.dirname(output_path)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as file:
            file.write(report)

        logging.info("گزارش ذخیره شد: %s", output_path)

    except OSError as exc:
        logging.exception("ذخیره گزارش ناموفق بود: %s", exc)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
