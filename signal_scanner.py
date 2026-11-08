
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ICT + Fibonacci LBank Signal Scanner
Analysis only: no order execution.

Timeframes:
4H / 1H = directional bias
15M = setup, structure, OTE
5M = entry trigger

Install:
    pip install requests

Run:
    python signal_scanner.py

Optional environment:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    SYMBOLS=BTC_USDT,ETH_USDT,SOL_USDT
    ACCOUNT_EQUITY=1000
    RISK_PCT=0.5
    MAX_LEVERAGE=2
    FEE_PCT=0.06
"""

import os
import sys
import time
import math
import html
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "LBANK_BASE_URL",
    "https://api.lbkex.com"
).rstrip("/")

DEFAULT_SYMBOLS = (
    "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,"
    "XRP_USDT,DOGE_USDT,ADA_USDT,AVAX_USDT,"
    "LINK_USDT,DOT_USDT,LTC_USDT"
)

SYMBOLS = [
    x.strip().upper()
    for x in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",")
    if x.strip()
]

RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "2"))
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "0"))
FEE_PCT = float(os.getenv("FEE_PCT", "0.06"))

MIN_NET_RR_TP1 = float(os.getenv("MIN_NET_RR_TP1", "1.2"))
MIN_NET_RR_TP2 = float(os.getenv("MIN_NET_RR_TP2", "2.0"))

MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.65"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "3.0"))
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "0.15"))

REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.25"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = {
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("ICT_SCANNER")

_session = requests.Session()
_session.headers.update({
    "User-Agent": "ICT-Fibonacci-Scanner/1.0",
    "Accept": "application/json",
})

_last_request = 0.0


# ============================================================
# HTTP AND MARKET DATA
# ============================================================

def api_get(path: str, params: Dict[str, Any]) -> Any:
    global _last_request

    wait = REQUEST_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)

    url = BASE_URL + path

    last_error = None

    for attempt in range(4):
        try:
            response = _session.get(
                url,
                params=params,
                timeout=15
            )
            _last_request = time.monotonic()

            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Invalid API response")

            return data

        except Exception as exc:
            last_error = str(exc)
            time.sleep(min(2 ** attempt, 10))

    raise RuntimeError(
        "LBank API error: " + str(last_error)
    )


def fetch_candles(
    symbol: str,
    timeframe: str,
    size: int = 300
) -> List[Dict[str, float]]:

    if timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")

    interval, seconds = TIMEFRAMES[timeframe]

    start = int(time.time()) - size * seconds

    data = api_get(
        "/v2/kline.do",
        {
            "symbol": symbol.lower(),
            "size": size,
            "type": interval,
            "time": start,
        }
    )

    if str(data.get("result", "")).lower() != "true":
        raise RuntimeError(
            "Kline API rejected " + symbol + " " + timeframe
        )

    raw = data.get("data", [])

    if not isinstance(raw, list):
        raise RuntimeError("Invalid candle list")

    candles = []

    for row in raw:
        try:
            ts = int(float(row[0]))

            if ts > 10**11:
                ts //= 1000

            candle = {
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }

            if (
                candle["low"] <= 0
                or candle["high"] < candle["low"]
                or candle["volume"] < 0
            ):
                continue

            candles.append(candle)

        except (ValueError, TypeError, IndexError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Remove currently forming candle.
    if candles:
        if candles[-1]["time"] + seconds > time.time():
            candles.pop()

    if len(candles) < 100:
        raise RuntimeError(
            "Not enough closed candles: " + timeframe
        )

    return candles


def fetch_price(symbol: str) -> float:
    data = api_get(
        "/v2/ticker/24hr.do",
        {"symbol": symbol.lower()}
    )

    items = data.get("data", [])

    if not items:
        raise RuntimeError("Ticker unavailable")

    ticker = items[0].get("ticker", {})

    price = float(ticker.get("latest", 0))

    if price <= 0:
        raise RuntimeError("Invalid ticker price")

    return price


def fetch_market(symbol: str) -> Dict[str, Any]:
    result = {}

    for tf in ("4h", "1h", "15m", "5m"):
        result[tf] = fetch_candles(symbol, tf)

    result["price"] = fetch_price(symbol)

    return result


# ============================================================
# GENERAL PRICE UTILITIES
# ============================================================

def candle_body(c: Dict[str, float]) -> float:
    return abs(c["close"] - c["open"])


def candle_range(c: Dict[str, float]) -> float:
    return max(c["high"] - c["low"], 1e-12)


def is_bullish(c: Dict[str, float]) -> bool:
    return c["close"] > c["open"]


def is_bearish(c: Dict[str, float]) -> bool:
    return c["close"] < c["open"]


def mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def atr(
    candles: List[Dict[str, float]],
    period: int = 14
) -> float:

    if len(candles) < period + 2:
        return 0.0

    values = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]

        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )

        values.append(tr)

    return mean(values[-period:])


def volume_ratio(
    candles: List[Dict[str, float]],
    period: int = 20
) -> float:

    if len(candles) < period + 2:
        return 0.0

    baseline = mean([
        c["volume"]
        for c in candles[-period-1:-1]
    ])

    if baseline <= 0:
        return 0.0

    return candles[-1]["volume"] / baseline


def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.5f}"
    return f"{value:.8f}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

# ============================================================
# MARKET STRUCTURE
# ============================================================

def find_swings(
    candles: List[Dict[str, float]],
    strength: int = 3
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:

    highs = []
    lows = []

    for i in range(strength, len(candles) - strength):
        high = candles[i]["high"]
        low = candles[i]["low"]

        left = candles[i-strength:i]
        right = candles[i+1:i+strength+1]

        if all(high > x["high"] for x in left + right):
            highs.append((i, high))

        if all(low < x["low"] for x in left + right):
            lows.append((i, low))

    return highs, lows


def market_structure(
    candles: List[Dict[str, float]]
) -> Dict[str, Any]:

    highs, lows = find_swings(candles)

    result = {
        "trend": "RANGE",
        "highs": highs,
        "lows": lows,
        "bos": None,
        "choch": None,
        "last_high": None,
        "last_low": None,
    }

    if len(highs) < 2 or len(lows) < 2:
        return result

    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]

    if h2 > h1 and l2 > l1:
        result["trend"] = "UP"

    elif h2 < h1 and l2 < l1:
        result["trend"] = "DOWN"

    result["last_high"] = highs[-1][1]
    result["last_low"] = lows[-1][1]

    close = candles[-1]["close"]

    if close > highs[-1][1]:
        result["bos"] = "BULLISH"

    elif close < lows[-1][1]:
        result["bos"] = "BEARISH"

    return result


def higher_timeframe_bias(
    candles_4h: List[Dict[str, float]],
    candles_1h: List[Dict[str, float]]
) -> str:

    s4 = market_structure(candles_4h)
    s1 = market_structure(candles_1h)

    if s4["trend"] == "UP" and s1["trend"] == "UP":
        return "LONG"

    if s4["trend"] == "DOWN" and s1["trend"] == "DOWN":
        return "SHORT"

    return "NONE"


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity_sweep(
    candles: List[Dict[str, float]],
    side: str,
    lookback: int = 20
) -> Optional[Dict[str, float]]:

    if len(candles) < lookback + 3:
        return None

    last = candles[-1]
    previous = candles[-lookback-1:-1]

    if side == "LONG":
        liquidity = max(x["high"] for x in previous)

        # Sweep below sell-side liquidity, then close back above it.
        if (
            last["low"] < liquidity
            and last["close"] > liquidity
        ):
            return {
                "level": liquidity,
                "extreme": last["low"],
                "close": last["close"],
            }

    else:
        liquidity = min(x["low"] for x in previous)

        # Sweep above buy-side liquidity, then close back below it.
        if (
            last["high"] > liquidity
            and last["close"] < liquidity
        ):
            return {
                "level": liquidity,
                "extreme": last["high"],
                "close": last["close"],
            }

    return None


# ============================================================
# MARKET STRUCTURE SHIFT / DISPLACEMENT
# ============================================================

def detect_mss(
    candles: List[Dict[str, float]],
    side: str,
    lookback: int = 12
) -> Optional[Dict[str, float]]:

    if len(candles) < lookback + 3:
        return None

    last = candles[-1]
    previous = candles[-lookback-1:-1]

    average_atr = atr(candles, 14)

    if average_atr <= 0:
        return None

    body = candle_body(last)

    # Require displacement, not a tiny candle break.
    if body < average_atr * 0.65:
        return None

    if side == "LONG":
        reference = max(x["high"] for x in previous)

        if (
            last["close"] > reference
            and is_bullish(last)
        ):
            return {
                "level": reference,
                "close": last["close"],
                "body": body,
            }

    else:
        reference = min(x["low"] for x in previous)

        if (
            last["close"] < reference
            and is_bearish(last)
        ):
            return {
                "level": reference,
                "close": last["close"],
                "body": body,
            }

    return None


# ============================================================
# FAIR VALUE GAP
# ============================================================

def find_fvg(
    candles: List[Dict[str, float]],
    side: str,
    lookback: int = 60
) -> Optional[Dict[str, float]]:

    start = max(2, len(candles) - lookback)

    for i in range(len(candles) - 1, start - 1, -1):
        first = candles[i - 2]
        third = candles[i]

        if side == "LONG":
            # Bullish FVG
            if third["low"] > first["high"]:
                return {
                    "low": first["high"],
                    "high": third["low"],
                    "mid": (
                        first["high"] + third["low"]
                    ) / 2,
                }

        else:
            # Bearish FVG
            if third["high"] < first["low"]:
                return {
                    "low": third["high"],
                    "high": first["low"],
                    "mid": (
                        third["high"] + first["low"]
                    ) / 2,
                }

    return None


# ============================================================
# ORDER BLOCK
# ============================================================

def find_order_block(
    candles: List[Dict[str, float]],
    side: str,
    lookback: int = 40
) -> Optional[Dict[str, float]]:

    start = max(0, len(candles) - lookback)

    for i in range(len(candles) - 2, start, -1):
        current = candles[i]
        next_candle = candles[i + 1]

        displacement = candle_body(next_candle)

        if side == "LONG":
            if (
                is_bearish(current)
                and is_bullish(next_candle)
                and next_candle["close"] > current["high"]
            ):
                return {
                    "low": current["low"],
                    "high": current["open"],
                    "mid": (
                        current["low"] + current["open"]
                    ) / 2,
                }

        else:
            if (
                is_bullish(current)
                and is_bearish(next_candle)
                and next_candle["close"] < current["low"]
            ):
                return {
                    "low": current["open"],
                    "high": current["high"],
                    "mid": (
                        current["open"] + current["high"]
                    ) / 2,
                }

    return None


# ============================================================
# FIBONACCI / PREMIUM / DISCOUNT / OTE
# ============================================================

def fibonacci_setup(
    candles: List[Dict[str, float]],
    side: str,
    price: float
) -> Dict[str, Any]:

    highs, lows = find_swings(candles)

    result = {
        "valid": False,
        "ote": False,
        "discount": False,
        "premium": False,
        "retracement": None,
        "zone_low": None,
        "zone_high": None,
        "ext_1272": None,
        "ext_1618": None,
        "swing_low": None,
        "swing_high": None,
    }

    if not highs or not lows:
        return result

    if side == "LONG":
        high_index, high = highs[-1]

        previous_lows = [
            x for x in lows
            if x[0] < high_index
        ]

        if not previous_lows:
            return result

        low_index, low = previous_lows[-1]

        impulse = high - low

        if impulse <= 0 or price >= high:
            return result

        retracement = (high - price) / impulse

        zone_low = high - 0.786 * impulse
        zone_high = high - 0.618 * impulse

        ext_1272 = low + 1.272 * impulse
        ext_1618 = low + 1.618 * impulse

    else:
        low_index, low = lows[-1]

        previous_highs = [
            x for x in highs
            if x[0] < low_index
        ]

        if not previous_highs:
            return result

        high_index, high = previous_highs[-1]

        impulse = high - low

        if impulse <= 0 or price <= low:
            return result

        retracement = (price - low) / impulse

        zone_low = low + 0.618 * impulse
        zone_high = low + 0.786 * impulse

        ext_1272 = high - 1.272 * impulse
        ext_1618 = high - 1.618 * impulse

    valid = 0 < retracement < 1

    result.update({
        "valid": valid,
        "ote": valid and 0.618 <= retracement <= 0.786,
        "discount": price < (high + low) / 2,
        "premium": price > (high + low) / 2,
        "retracement": retracement,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "ext_1272": ext_1272,
        "ext_1618": ext_1618,
        "swing_low": low,
        "swing_high": high,
    })

    return result


def zone_overlap(
    price: float,
    fib: Dict[str, Any],
    ob: Optional[Dict[str, float]],
    fvg: Optional[Dict[str, float]]
) -> bool:

    if not fib["valid"]:
        return False

    zlow = fib["zone_low"]
    zhigh = fib["zone_high"]

    for zone in (ob, fvg):
        if not zone:
            continue

        if (
            max(zlow, zone["low"])
            <= min(zhigh, zone["high"])
        ):
            return True

    return False

# ============================================================
# ICT SETUP ENGINE
# ============================================================

def analyze_side(
    market: Dict[str, Any],
    side: str
) -> Dict[str, Any]:

    c4 = market["4h"]
    c1 = market["1h"]
    c15 = market["15m"]
    c5 = market["5m"]
    price = market["price"]

    reasons = []

    # --------------------------------------------------------
    # 1. Higher timeframe direction
    # --------------------------------------------------------

    bias = higher_timeframe_bias(c4, c1)

    if bias != side:
        return {
            "valid": False,
            "reason": "جهت 4H و 1H با این معامله هم‌جهت نیست",
        }

    # --------------------------------------------------------
    # 2. 15m structure
    # --------------------------------------------------------

    structure15 = market_structure(c15)

    if structure15["trend"] != (
        "UP" if side == "LONG" else "DOWN"
    ):
        return {
            "valid": False,
            "reason": "ساختار 15M تأییدکننده جهت معامله نیست",
        }

    # --------------------------------------------------------
    # 3. Fibonacci OTE
    # --------------------------------------------------------

    fib = fibonacci_setup(c15, side, price)

    if not fib["valid"]:
        return {
            "valid": False,
            "reason": "موج معتبر برای محاسبه فیبوناچی وجود ندارد",
        }

    if not fib["ote"]:
        return {
            "valid": False,
            "reason": "قیمت در محدوده OTE فیبوناچی نیست",
        }

    # LONG should be in discount.
    if side == "LONG" and not fib["discount"]:
        return {
            "valid": False,
            "reason": "برای لانگ قیمت در Discount نیست",
        }

    # SHORT should be in premium.
    if side == "SHORT" and not fib["premium"]:
        return {
            "valid": False,
            "reason": "برای شورت قیمت در Premium نیست",
        }

    # --------------------------------------------------------
    # 4. OB / FVG confluence
    # --------------------------------------------------------

    ob = find_order_block(c15, side)
    fvg = find_fvg(c15, side)

    overlap = zone_overlap(
        price,
        fib,
        ob,
        fvg
    )

    if not overlap:
        return {
            "valid": False,
            "reason": "OTE با Order Block یا FVG هم‌پوشانی ندارد",
        }

    reasons.append("OTE + OB/FVG")

    # --------------------------------------------------------
    # 5. 5m liquidity sweep
    # --------------------------------------------------------

    sweep = detect_liquidity_sweep(
        c5,
        side,
        lookback=20
    )

    if not sweep:
        return {
            "valid": False,
            "reason": "Liquidity Sweep در 5M تأیید نشده",
        }

    reasons.append("Liquidity Sweep")

    # --------------------------------------------------------
    # 6. 5m MSS and displacement
    # --------------------------------------------------------

    mss = detect_mss(
        c5,
        side,
        lookback=12
    )

    if not mss:
        return {
            "valid": False,
            "reason": "MSS همراه با Displacement در 5M نداریم",
        }

    reasons.append("MSS + Displacement")

    # --------------------------------------------------------
    # 7. Volume confirmation
    # --------------------------------------------------------

    vol_ratio = volume_ratio(c5)

    if vol_ratio < MIN_VOLUME_RATIO:
        return {
            "valid": False,
            "reason": "حجم معاملات برای تأیید تریگر ضعیف است",
        }

    # --------------------------------------------------------
    # 8. Entry and structural stop
    # --------------------------------------------------------

    entry = c5[-1]["close"]

    average_atr = atr(c5)

    if average_atr <= 0:
        return {
            "valid": False,
            "reason": "ATR نامعتبر است",
        }

    buffer = average_atr * 0.15

    if side == "LONG":
        stop = sweep["extreme"] - buffer
        risk = entry - stop
    else:
        stop = sweep["extreme"] + buffer
        risk = stop - entry

    if risk <= 0:
        return {
            "valid": False,
            "reason": "حد ضرر ساختاری معتبر نیست",
        }

    stop_pct = risk / entry * 100

    if stop_pct < MIN_STOP_PCT:
        return {
            "valid": False,
            "reason": "فاصله حد ضرر بیش از حد کوچک است",
        }

    if stop_pct > MAX_STOP_PCT:
        return {
            "valid": False,
            "reason": "فاصله حد ضرر بیش از حد بزرگ است",
        }

    # --------------------------------------------------------
    # 9. Structural and Fibonacci targets
    # --------------------------------------------------------

    highs, lows = find_swings(c15)

    if side == "LONG":
        structural_targets = sorted(
            set(
                p for _, p in highs
                if p > entry
            )
        )

        fib_targets = [
            x for x in (
                fib["ext_1272"],
                fib["ext_1618"]
            )
            if x and x > entry
        )

    else:
        structural_targets = sorted(
            set(
                p for _, p in lows
                if p < entry
            ),
            reverse=True
        )

        fib_targets = [
            x for x in (
                fib["ext_1272"],
                fib["ext_1618"]
            )
            if x and x < entry
        ]

    candidates = sorted(
        set(structural_targets + fib_targets),
        reverse=(side == "SHORT")
    )

    # Keep only targets beyond entry and with enough gross distance.
    valid_targets = []

    for target in candidates:
        reward = (
            target - entry
            if side == "LONG"
            else entry - target
        )

        if reward <= 0:
            continue

        gross_rr = reward / risk

        if gross_rr >= 1.25:
            valid_targets.append(target)

    if len(valid_targets) < 2:
        return {
            "valid": False,
            "reason": "دو تارگت ساختاری معتبر با RR کافی وجود ندارد",
        }

    tp1 = valid_targets[0]
    tp2 = valid_targets[1]

    # --------------------------------------------------------
    # 10. Net RR after round-trip fee estimate
    # --------------------------------------------------------

    fee_rate = FEE_PCT / 100.0

    entry_cost = entry * fee_rate
    exit_cost_1 = tp1 * fee_rate
    exit_cost_2 = tp2 * fee_rate

    total_cost_1 = entry_cost + exit_cost_1
    total_cost_2 = entry_cost + exit_cost_2

    net_risk = risk + total_cost_1

    reward_1 = (
        tp1 - entry
        if side == "LONG"
        else entry - tp1
    )

    reward_2 = (
        tp2 - entry
        if side == "LONG"
        else entry - tp2
    )

    net_reward_1 = reward_1 - total_cost_1
    net_reward_2 = reward_2 - total_cost_2

    if net_risk <= 0 or net_reward_1 <= 0:
        return {
            "valid": False,
            "reason": "پس از کسر کارمزد، سود خالص معتبر نیست",
        }

    rr1 = net_reward_1 / net_risk
    rr2 = net_reward_2 / net_risk

    if rr1 < MIN_NET_RR_TP1:
        return {
            "valid": False,
            "reason": "RR خالص تارگت اول کمتر از حد مجاز است",
        }

    if rr2 < MIN_NET_RR_TP2:
        return {
            "valid": False,
            "reason": "RR خالص تارگت دوم کمتر از حد مجاز است",
        }

    # --------------------------------------------------------
    # 11. Final setup
    # --------------------------------------------------------

    return {
        "valid": True,
        "side": side,
        "entry": entry,
        "sl": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "stop_pct": stop_pct,
        "rr1": rr1,
        "rr2": rr2,
        "fib": fib,
        "sweep": sweep,
        "mss": mss,
        "ob": ob,
        "fvg": fvg,
        "volume_ratio": vol_ratio,
        "reasons": reasons,
    }


# ============================================================
# POSITION SIZE AND LEVERAGE
# ============================================================

def calculate_position(
    trade: Dict[str, Any]
) -> Dict[str, float]:

    stop_pct = trade["stop_pct"] / 100.0

    leverage = min(
        MAX_LEVERAGE,
        1.0 / max(stop_pct, 1e-9)
    )

    leverage = max(1.0, leverage)

    if ACCOUNT_EQUITY <= 0:
        return {
            "leverage": leverage,
            "risk_usdt": 0.0,
            "margin_usdt": 0.0,
            "notional_usdt": 0.0,
            "quantity": 0.0,
        }

    risk_usdt = ACCOUNT_EQUITY * RISK_PCT / 100.0

    # Quantity is sized so that stop loss approximately
    # equals the configured equity risk.
    quantity = risk_usdt / trade["risk"]

    notional = quantity * trade["entry"]

    max_notional = ACCOUNT_EQUITY * leverage

    if notional > max_notional:
        notional = max_notional
        quantity = notional / trade["entry"]

    margin = notional / leverage

    return {
        "leverage": leverage,
        "risk_usdt": risk_usdt,
        "margin_usdt": margin,
        "notional_usdt": notional,
        "quantity": quantity,
    }

# ============================================================
# COMPLETE SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(symbol: str) -> Dict[str, Any]:

    market = fetch_market(symbol)

    long_result = analyze_side(market, "LONG")
    short_result = analyze_side(market, "SHORT")

    valid = [
        x for x in (long_result, short_result)
        if x.get("valid")
    ]

    # No ambiguous dual-direction signal.
    if len(valid) == 1:
        trade = valid[0]

        position = calculate_position(trade)

        return {
            "symbol": symbol,
            "price": market["price"],
            "status": "SIGNAL",
            "trade": trade,
            "position": position,
            "reason": "",
        }

    if len(valid) == 2:
        return {
            "symbol": symbol,
            "price": market["price"],
            "status": "NO_TRADE",
            "trade": None,
            "position": None,
            "reason": "هر دو جهت هم‌زمان معتبر تشخیص داده شدند؛ ورود نکن",
        }

    # Give the most informative failure reason.
    long_reason = long_result.get(
        "reason",
        "ستاپ لانگ کامل نیست"
    )

    short_reason = short_result.get(
        "reason",
        "ستاپ شورت کامل نیست"
    )

    return {
        "symbol": symbol,
        "price": market["price"],
        "status": "NO_TRADE",
        "trade": None,
        "position": None,
        "reason": (
            "لانگ: " + long_reason
            + " | شورت: " + short_reason
        ),
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str) -> bool:

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        response.raise_for_status()

        result = response.json()

        return bool(result.get("ok"))

    except Exception as exc:
        log.error("Telegram error: %s", exc)
        return False


def format_signal(result: Dict[str, Any]) -> str:

    trade = result["trade"]
    pos = result["position"]

    side = trade["side"]

    direction = (
        "🟢 LONG"
        if side == "LONG"
        else "🔴 SHORT"
    )

    reasons = "\n".join(
        "• " + html.escape(x)
        for x in trade["reasons"]
    )

    fib = trade["fib"]

    if pos["quantity"] > 0:
        position_text = (
            f"\nریسک حساب: {pos['risk_usdt']:.2f} USDT"
            f"\nحجم اسمی: {pos['notional_usdt']:.2f} USDT"
            f"\nمارجین تقریبی: {pos['margin_usdt']:.2f} USDT"
            f"\nتعداد: {pos['quantity']:.6f}"
        )
    else:
        position_text = (
            "\nبرای محاسبه حجم، ACCOUNT_EQUITY را تنظیم کن."
        )

    return (
        f"🎯 <b>ICT TRADE SIGNAL</b>\n"
        f"\n<b>{html.escape(result['symbol'])}</b>"
        f" | {direction}\n"
        f"\nورود: <code>{fmt_price(trade['entry'])}</code>"
        f"\nحد ضرر: <code>{fmt_price(trade['sl'])}</code>"
        f"\nTP1: <code>{fmt_price(trade['tp1'])}</code>"
        f"\nTP2: <code>{fmt_price(trade['tp2'])}</code>"
        f"\n\nRR خالص TP1: <b>{trade['rr1']:.2f}</b>"
        f"\nRR خالص TP2: <b>{trade['rr2']:.2f}</b>"
        f"\nفاصله حد ضرر: {trade['stop_pct']:.2f}%"
        f"\nکارمزد هر سمت: {FEE_PCT:.3f}%"
        f"\n\nاهرم پیشنهادی: {pos['leverage']:.2f}x"
        f"{position_text}"
        f"\n\n<b>دلایل ستاپ:</b>\n{reasons}"
        f"\n\nفیبوناچی اصلاح: {fib['retracement']:.3f}"
        f"\nOTE: تأیید شده"
        f"\n\n⚠️ اهرم بالا خطر لیکوییدشدن دارد."
        f"\n⚠️ سیگنال تضمین سود نیست؛ قبل از معامله قیمت را بررسی کن."
    )


def format_no_trade(result: Dict[str, Any]) -> str:

    return (
        f"⛔ <b>ورود نکن | NO TRADE</b>\n"
        f"\nنماد: <b>{html.escape(result['symbol'])}</b>"
        f"\nقیمت فعلی: <code>{fmt_price(result['price'])}</code>"
        f"\n\nدلیل:\n{html.escape(result['reason'])}"
    )


def format_error(symbol: str, error: str) -> str:

    return (
        f"⚠️ <b>خطای تحلیل</b>\n"
        f"\nنماد: {html.escape(symbol)}"
        f"\nجزئیات: {html.escape(error[:300])}"
        f"\n\nبه دلیل خطای داده، هیچ سیگنالی صادر نشد."
    )


# ============================================================
# REPORT
# ============================================================

def scan_all() -> List[Dict[str, Any]]:

    results = []

    for symbol in SYMBOLS:
        log.info("Scanning %s", symbol)

        try:
            result = analyze_symbol(symbol)
            results.append(result)

        except Exception as exc:
            log.exception("Analysis failed for %s", symbol)

            results.append({
                "symbol": symbol,
                "price": 0.0,
                "status": "ERROR",
                "trade": None,
                "position": None,
                "reason": str(exc),
            })

    return results


def send_results(results: List[Dict[str, Any]]) -> None:

    signals = [
        x for x in results
        if x["status"] == "SIGNAL"
    ]

    if signals:
        for result in signals:
            send_telegram(format_signal(result))

    # One concise NO TRADE report for symbols without valid setups.
    no_trades = [
        x for x in results
        if x["status"] == "NO_TRADE"
    ]

    if no_trades:
        lines = [
            "⛔ <b>ورود نکن | گزارش نمادهای بدون ستاپ</b>",
            "",
        ]

        for item in no_trades:
            lines.append(
                f"<b>{html.escape(item['symbol'])}</b>: "
                f"{html.escape(item['reason'][:250])}"
            )

        send_telegram("\n".join(lines))

    errors = [
        x for x in results
        if x["status"] == "ERROR"
    ]

    for item in errors:
        send_telegram(
            format_error(
                item["symbol"],
                item["reason"]
            )
        )

    if not signals:
        send_telegram(
            "📌 <b>نتیجه اسکن:</b> هیچ معامله معتبری پیدا نشد."
            "\n\nقانون سیستم: در نبود ستاپ کامل، ورود نکن."
        )

# ============================================================
# VALIDATION
# ============================================================

def validate_config() -> None:

    if not SYMBOLS:
        raise ValueError("SYMBOLS is empty")

    if RISK_PCT <= 0 or RISK_PCT > 2:
        raise ValueError(
            "RISK_PCT must be greater than 0 and at most 2"
        )

    if MAX_LEVERAGE < 1 or MAX_LEVERAGE > 5:
        raise ValueError(
            "MAX_LEVERAGE must be between 1 and 5"
        )

    if FEE_PCT < 0 or FEE_PCT > 1:
        raise ValueError("Invalid FEE_PCT")

    if MIN_NET_RR_TP1 <= 0 or MIN_NET_RR_TP2 <= 0:
        raise ValueError("Invalid RR configuration")

    if ACCOUNT_EQUITY < 0:
        raise ValueError("ACCOUNT_EQUITY cannot be negative")


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    try:
        validate_config()

    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    log.info("Starting ICT + Fibonacci scanner")
    log.info("Symbols: %s", ", ".join(SYMBOLS))
    log.info("Risk per trade: %.2f%%", RISK_PCT)
    log.info("Maximum leverage: %.2fx", MAX_LEVERAGE)

    results = scan_all()

    if not results:
        log.error("No scan results")
        return 1

    send_results(results)

    signals = sum(
        1 for x in results
        if x["status"] == "SIGNAL"
    )

    no_trades = sum(
        1 for x in results
        if x["status"] == "NO_TRADE"
    )

    errors = sum(
        1 for x in results
        if x["status"] == "ERROR"
    )

    log.info(
        "Finished | signals=%d | no_trade=%d | errors=%d",
        signals,
        no_trades,
        errors,
    )

    return 0


if __name__ == "__main__":

    try:
        sys.exit(main())

    except KeyboardInterrupt:
        log.warning("Scanner interrupted")
        sys.exit(130)
