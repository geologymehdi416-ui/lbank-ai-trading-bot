#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Scanner PRO — Multi-Exchange USDT-M Futures edition.

Analysis only: public market data, no order placement, no API keys.
Exchanges supported (subject to CCXT market/data availability):
  binanceusdm, bybit, okx, kucoinfutures

The scanner evaluates each exchange independently, then reports cross-exchange
agreement. It uses closed candles only. Structure/SMC labels are rule-based
approximations and must be validated with backtests and paper trading.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import ccxt
    import numpy as np
    import pandas as pd
except ImportError as exc:
    raise SystemExit("Install dependencies first: pip install -r requirements.txt") from exc

LOG = logging.getLogger("signal_scanner")
TIMEFRAMES = ("4h", "1h", "15m", "5m")
DEFAULT_EXCHANGES = ["binanceusdm", "bybit", "okx", "kucoinfutures"]


class Config:
    def __init__(self, **kwargs):
        defaults = {
            "exchanges": DEFAULT_EXCHANGES,
            "symbols": None,
            "quote": "USDT",
            "scan_limit": 80,
            "ohlcv_limit": 300,
            "min_score": 75,
            "strong_score": 85,
            "risk_pct": 0.005,
            "account_equity": 1000.0,
            "fee_rate": 0.0005,
            "slippage_rate": 0.0005,
            "min_rr_tp1": 1.2,
            "min_rr_tp2": 1.8,
            "min_volume_ratio": 1.10,
            "atr_sl_buffer": 0.25,
            "max_sl_pct": 0.035,
            "min_sl_pct": 0.0025,
            "pivot_left": 3,
            "pivot_right": 3,
            "equal_level_atr": 0.15,
            "cooldown_seconds": 0.12,
            "output": "signals_futures_multi.json",
        }
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sf(x: Any, default: float = 0.0) -> float:
    try:
        y = float(x)
        return y if math.isfinite(y) else default
    except (TypeError, ValueError):
        return default


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100/(1+rs)
    # A one-way rising series has RSI=100; a flat series has RSI=50.
    out = out.where(loss != 0, np.where(gain > 0, 100.0, 50.0))
    return out.fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift(1)
    tr = pd.concat([
        (df.high-df.low),
        (df.high-pc).abs(),
        (df.low-pc).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema20"], d["ema50"], d["ema200"] = (
        ema(d.close, 20), ema(d.close, 50), ema(d.close, 200)
    )
    d["rsi"] = rsi(d.close)
    d["macd"] = ema(d.close, 12)-ema(d.close, 26)
    d["macd_signal"] = ema(d.macd, 9)
    d["macd_hist"] = d.macd-d.macd_signal
    d["atr"] = atr(d)
    d["vol_sma20"] = d.volume.rolling(20).mean()
    d["vol_ratio"] = d.volume/d.vol_sma20.replace(0, np.nan)
    return d.replace([np.inf, -np.inf], np.nan)


def pivots(df: pd.DataFrame, left: int = 3, right: int = 3):
    hs, ls = [], []
    h, l = df.high.to_numpy(), df.low.to_numpy()
    for i in range(left, len(df)-right):
        if h[i] > np.max(h[i-left:i]) and h[i] >= np.max(h[i+1:i+right+1]):
            hs.append((i, float(h[i])))
        if l[i] < np.min(l[i-left:i]) and l[i] <= np.min(l[i+1:i+right+1]):
            ls.append((i, float(l[i])))
    return hs, ls
def cluster(vals: List[float], tol: float) -> List[float]:
    if not vals:
        return []
    vals = sorted(vals)
    groups = [[vals[0]]]
    for v in vals[1:]:
        if abs(v-np.mean(groups[-1])) <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def levels(df: pd.DataFrame, cfg: Config) -> Dict[str, List[float]]:
    hs, ls = pivots(df, cfg.pivot_left, cfg.pivot_right)
    hs = [v for i, v in hs if i >= len(df)-120]
    ls = [v for i, v in ls if i >= len(df)-120]
    last = df.iloc[-1]
    tol = max(
        sf(last.atr)*cfg.equal_level_atr,
        sf(last.close)*0.0005
    )
    return {
        "resistance": cluster(hs, tol),
        "support": cluster(ls, tol),
        "swing_highs": hs[-10:],
        "swing_lows": ls[-10:]
    }


def trend(df: pd.DataFrame) -> str:
    x = df.iloc[-1]
    if x.close > x.ema20 > x.ema50 > x.ema200:
        return "BULLISH"
    if x.close < x.ema20 < x.ema50 < x.ema200:
        return "BEARISH"
    return "RANGE"


def structure(df: pd.DataFrame, cfg: Config) -> Dict[str, Any]:
    # Pivot points are confirmed only after cfg.pivot_right bars.
    hs, ls = pivots(df.iloc[:-1], cfg.pivot_left, cfg.pivot_right)
    x = df.iloc[-1]
    ph = hs[-1][1] if hs else None
    pl = ls[-1][1] if ls else None

    # Count a sweep only when the latest CLOSED candle itself raids and reclaims
    # the confirmed swing. Older raids must not remain valid for several bars.
    sweep_low = bool(
        pl is not None and x.low < pl and x.close > pl and x.close > x.open
    )
    sweep_high = bool(
        ph is not None and x.high > ph and x.close < ph and x.close < x.open
    )

    bull_bos = bool(ph and x.close > ph)
    bear_bos = bool(pl and x.close < pl)

    # Practical CHoCH/MSS proxy, not a claim of institutional order flow.
    return {
        "swing_high": ph,
        "swing_low": pl,
        "sweep_low": sweep_low,
        "sweep_high": sweep_high,
        "bull_bos": bull_bos,
        "bear_bos": bear_bos,
        "bull_mss": bool(sweep_low and bull_bos),
        "bear_mss": bool(sweep_high and bear_bos)
    }


def fvg(df: pd.DataFrame, side: str):
    found = []
    for i in range(max(2, len(df)-35), len(df)):
        a, c = df.iloc[i-2], df.iloc[i]
        if side == "LONG" and c.low > a.high:
            found.append((float(a.high), float(c.low)))
        if side == "SHORT" and c.high < a.low:
            found.append((float(c.high), float(a.low)))
    return found[-1] if found else None


def order_block(df: pd.DataFrame, side: str):
    d = df.iloc[:-1]
    for i in range(len(d)-2, max(2, len(d)-30), -1):
        c, n = d.iloc[i], d.iloc[i+1]
        av = sf(d.iloc[max(0, i-20):i].atr.mean())
        if av <= 0 or abs(n.close-n.open) < 1.2*av:
            continue
        if side == "LONG" and c.close < c.open and n.close > n.open:
            return (float(c.low), float(c.high))
        if side == "SHORT" and c.close > c.open and n.close < n.open:
            return (float(c.low), float(c.high))
    return None


def fib_zone(df: pd.DataFrame, side: str):
    hs, ls = pivots(df.iloc[:-1], 3, 3)
    if not hs or not ls:
        return None

    hi_i, hi = hs[-1]
    lo_i, lo = ls[-1]

    if side == "LONG" and lo_i < hi_i and hi > lo:
        return (hi-.618*(hi-lo), hi-.5*(hi-lo))
    if side == "SHORT" and hi_i < lo_i and hi > lo:
        return (lo+.5*(hi-lo), lo+.618*(hi-lo))
    return None


def near_any_zone(
    price: float,
    zones: List[Optional[Tuple[float, float]]],
    atr_v: float
) -> bool:
    for z in zones:
        if z and (
            min(z) <= price <= max(z)
            or min(abs(price-z[0]), abs(price-z[1])) <= 0.5*atr_v
        ):
            return True
    return False


def make_exchange(exchange_id: str):
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"CCXT does not provide exchange id '{exchange_id}'")
    cls = getattr(ccxt, exchange_id)
    return cls({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear"
        }
    })


def normalize_symbol(exchange, raw: str) -> str:
    s = raw.strip().upper().replace("_", "/")
    if ":" in s:
        return s
    if "/" not in s:
        s = s[:-4]+"/"+s[-4:] if s.endswith("USDT") else s
    # Unified linear perpetual convention on CCXT is BASE/QUOTE:SETTLE.
    if s.endswith("/USDT"):
        s += ":USDT"
    return s
def load_symbols(exchange, cfg: Config) -> List[str]:
    markets = exchange.load_markets()

    if cfg.symbols:
        wanted = [normalize_symbol(exchange, s) for s in cfg.symbols]
        available = []

        for s in wanted:
            m = markets.get(s)
            valid = bool(
                m
                and m.get("active", True)
                and m.get("swap")
                and m.get("linear")
                and m.get("quote") == cfg.quote
                and m.get("settle") == cfg.quote
                and m.get("expiry") is None
            )

            if valid:
                available.append(s)
            else:
                LOG.warning(
                    "%s: not an active linear %s perpetual; skipped",
                    s, cfg.quote
                )
        return available

    pool = []
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        if not (m.get("swap") or m.get("future")):
            continue
        if not m.get("linear"):
            continue
        if m.get("quote") != cfg.quote or m.get("settle") not in (None, cfg.quote):
            continue
        if m.get("expiry") is not None:
            continue
        pool.append(sym)

    if not pool:
        return []

    try:
        tickers = exchange.fetch_tickers(pool)
    except Exception as batch_error:
        LOG.warning(
            "Bulk ticker request failed (%s); falling back to individual tickers",
            batch_error
        )
        tickers = {}

        # Bounded fallback avoids thousands of requests.
        fallback_pool = pool[:max(cfg.scan_limit*4, 200)]
        for sym in fallback_pool:
            try:
                tickers[sym] = exchange.fetch_ticker(sym)
            except Exception:
                continue

    ranked = sorted(
        [s for s in pool if s in tickers],
        key=lambda s: sf(tickers[s].get("quoteVolume")),
        reverse=True
    )
    return ranked[:cfg.scan_limit]


def fetch_df(exchange, symbol, tf, limit):
    raw = exchange.fetch_ohlcv(
        symbol,
        timeframe=tf,
        limit=limit
    )

    if not raw or len(raw) < 220:
        raise ValueError(f"not enough {tf} candles")

    df = pd.DataFrame(
        raw,
        columns=[
            "timestamp", "open", "high",
            "low", "close", "volume"
        ]
    )

    df = (
        df.drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    # Exclude currently forming candle.
    df = df.iloc[:-1].copy()

    numeric = ["open", "high", "low", "close", "volume"]
    df[numeric] = df[numeric].apply(
        pd.to_numeric,
        errors="coerce"
    )
    df = df.dropna(subset=numeric)

    if (
        (df[["open", "high", "low", "close"]] <= 0).any().any()
        or (df.volume < 0).any()
    ):
        raise ValueError(f"invalid OHLCV values on {tf}")

    if (
        df.high < df[["open", "close", "low"]].max(axis=1)
    ).any() or (
        df.low > df[["open", "close", "high"]].min(axis=1)
    ).any():
        raise ValueError(f"inconsistent OHLC values on {tf}")

    tf_ms = {
        "5m": 300000,
        "15m": 900000,
        "1h": 3600000,
        "4h": 14400000
    }[tf]

    gaps = df.timestamp.diff().dropna()
    if not gaps.empty and (gaps > tf_ms*1.8).any():
        raise ValueError(f"missing candle gap detected on {tf}")

    if len(df) < 220:
        raise ValueError(
            f"not enough closed {tf} candles after validation"
        )

    # Reject stale market data.
    age_ms = int(time.time()*1000)-int(df.timestamp.iloc[-1])
    if age_ms > tf_ms*2.5:
        raise ValueError(
            f"stale {tf} candles ({age_ms/60000:.1f} minutes old)"
        )

    df["datetime"] = pd.to_datetime(
        df.timestamp,
        unit="ms",
        utc=True
    )

    d = enrich(df).dropna(
        subset=["ema200", "atr", "rsi", "macd_hist"]
    )

    if len(d) < 30:
        raise ValueError(
            f"not enough valid {tf} rows after indicators"
        )

    return d


def funding_context(exchange, symbol):
    out = {}

    try:
        fr = exchange.fetch_funding_rate(symbol)
        out["funding_rate"] = sf(fr.get("fundingRate"), None)
        out["funding_timestamp"] = fr.get("timestamp")
    except Exception:
        pass

    try:
        oi = exchange.fetch_open_interest(symbol)
        out["open_interest"] = sf(oi.get("openInterest"), None)
        out["open_interest_value"] = sf(
            oi.get("openInterestValue"), None
        )
    except Exception:
        pass

    return out


def make_targets(side, entry, risk, levs):
    if side == "LONG":
        structural = sorted(
            x for x in levs["resistance"] if x > entry
        )
        fallbacks = [
            entry+risk*1.5,
            entry+risk*2.5,
            entry+risk*3.5
        ]
    else:
        structural = sorted(
            (x for x in levs["support"] if x < entry),
            reverse=True
        )
        fallbacks = [
            entry-risk*1.5,
            entry-risk*2.5,
            entry-risk*3.5
        ]

    targets = []
    for x in structural+fallbacks:
        if x <= 0:
            continue

        if not targets or (
            x > targets[-1] if side == "LONG"
            else x < targets[-1]
        ):
            targets.append(float(x))

        if len(targets) == 3:
            break

    return targets
def analyze(symbol, exchange_id, exchange, frames, cfg):
    d4, d1, d15, d5 = (frames[t] for t in TIMEFRAMES)

    px = sf(d5.iloc[-1].close)
    av = sf(d5.iloc[-1].atr)

    t4, t1 = trend(d4), trend(d1)
    st1, st15, st5 = (
        structure(d, cfg) for d in (d1, d15, d5)
    )

    lv = levels(d1, cfg)
    market = exchange.market(symbol)
    contract_size = sf(market.get("contractSize"), 1.0) or 1.0

    candidates = []

    for side in ("LONG", "SHORT"):
        bullish = side == "LONG"

        # Require directional agreement on both higher timeframes.
        aligned = (
            t4 == "BULLISH" and t1 == "BULLISH"
        ) if bullish else (
            t4 == "BEARISH" and t1 == "BEARISH"
        )

        sweep = (
            st15["sweep_low"] or st5["sweep_low"]
        ) if bullish else (
            st15["sweep_high"] or st5["sweep_high"]
        )

        bos = (
            st15["bull_bos"] or st5["bull_bos"]
        ) if bullish else (
            st15["bear_bos"] or st5["bear_bos"]
        )

        mss = (
            st15["bull_mss"] or st5["bull_mss"]
        ) if bullish else (
            st15["bear_mss"] or st5["bear_mss"]
        )

        fv = fvg(d5, side) or fvg(d15, side)
        ob = order_block(d5, side) or order_block(d15, side)
        fb = fib_zone(d15, side)

        x5, x15 = d5.iloc[-1], d15.iloc[-1]

        vol_ok = max(
            sf(x5.vol_ratio),
            sf(x15.vol_ratio)
        ) >= cfg.min_volume_ratio

        mom_ok = (
            x5.rsi >= 50 and x5.macd_hist > 0
        ) if bullish else (
            x5.rsi <= 50 and x5.macd_hist < 0
        )

        ema_ok = (
            x5.close > x5.ema20 > x5.ema50
        ) if bullish else (
            x5.close < x5.ema20 < x5.ema50
        )

        zone_ok = near_any_zone(
            px,
            [fv, ob, fb],
            av
        )

        score = (
            (15 if aligned else 0)
            + (20 if (bos or mss) else 0)
            + (15 if sweep else 0)
            + (15 if (fv or ob) else 0)
            + (10 if vol_ok else 0)
            + (10 if mom_ok else 0)
            + (5 if ema_ok else 0)
            + (
                10 if aligned and (bos or mss)
                and (sweep or vol_ok) else 0
            )
        )

        # Conservative structural stop.
        if bullish:
            swing = st15["swing_low"] or st5["swing_low"]
            candidates_sl = [
                v for v in (swing, px-av)
                if v and v < px
            ]

            sl = (
                min(candidates_sl)-cfg.atr_sl_buffer*av
                if candidates_sl else px-1.5*av
            )
            risk = px-sl

        else:
            swing = st15["swing_high"] or st5["swing_high"]
            candidates_sl = [
                v for v in (swing, px+av)
                if v and v > px
            ]

            sl = (
                max(candidates_sl)+cfg.atr_sl_buffer*av
                if candidates_sl else px+1.5*av
            )
            risk = sl-px

        sl_pct = risk/px if px else 1

        hard_reasons = []

        if not aligned:
            hard_reasons.append(
                "higher-timeframe trend not aligned"
            )

        if not (bos or mss):
            hard_reasons.append(
                "no confirmed BOS/CHoCH proxy"
            )

        if not sweep:
            hard_reasons.append(
                "no liquidity sweep"
            )

        if not vol_ok:
            hard_reasons.append(
                "volume below threshold"
            )

        if not mom_ok:
            hard_reasons.append(
                "RSI/MACD momentum not confirmed"
            )

        if not zone_ok:
            hard_reasons.append(
                "price not near OB/FVG/Fibonacci zone"
            )

        if sl_pct < cfg.min_sl_pct or sl_pct > cfg.max_sl_pct:
            hard_reasons.append(
                "stop distance outside configured limits"
            )

        if hard_reasons:
            candidates.append({
                "side": side,
                "score": score,
                "valid": False,
                "reasons": hard_reasons
            })
            continue

        targets = make_targets(side, px, risk, lv)
        rr = [abs(t-px)/risk for t in targets]

        if (
            len(targets) < 3
            or rr[0] < cfg.min_rr_tp1
            or rr[1] < cfg.min_rr_tp2
        ):
            candidates.append({
                "side": side,
                "score": score,
                "valid": False,
                "reasons": [
                    "structural target/RR insufficient"
                ]
            })
            continue

        risk_budget = cfg.account_equity*cfg.risk_pct

        # Estimated risk includes entry and exit fees/slippage.
        all_in_move = (
            risk + px*(
                2*cfg.fee_rate + 2*cfg.slippage_rate
            )
        )

        contracts = (
            risk_budget/(all_in_move*contract_size)
            if all_in_move > 0 else 0
        )

        notional = contracts*contract_size*px

        candidates.append({
            "side": side,
            "score": min(score, 100),
            "valid": score >= cfg.min_score,

            # Monitoring zone, NOT an instruction to enter at market.
            "watch_zone": [
                px-0.15*av,
                px+0.15*av
            ],
            "reference_price": px,

            "entry_instruction":
                "WAIT_FOR_RETEST_AND_CLOSED_CANDLE_CONFIRMATION",

            "stop_loss": sl,
            "targets": targets,
            "rr": rr,
            "stop_pct": sl_pct*100,

            "risk_budget_quote": risk_budget,
            "contracts": contracts,
            "contract_size": contract_size,
            "notional_quote": notional,
            "effective_exposure_x":
                notional/cfg.account_equity,

            "liquidity_sweep": sweep,
            "bos": bos,
            "mss_proxy": mss,

            "volume_ratio_5m": sf(x5.vol_ratio),
            "rsi_5m": sf(x5.rsi),
            "macd_hist_5m": sf(x5.macd_hist),
            "atr_5m": av,

            "zones": {
                "fvg": fv,
                "order_block": ob,
                "fibonacci": fb
            },

            "support_resistance": lv,

            "reasons": [
                "all mandatory filters passed; "
                "wait for retest and closed-candle confirmation"
            ]
        })

    valid = [c for c in candidates if c["valid"]]

    best = (
        max(valid, key=lambda x: x["score"])
        if valid else
        max(candidates, key=lambda x: x["score"])
    )

    status = "SETUP" if valid else "NO_TRADE"

    return {
        "exchange": exchange_id,
        "symbol": symbol,
        "status": status,
        "side": best["side"],
        "score": best["score"],
        "price": px,

        "trend": {
            "4h": t4,
            "1h": t1,
            "15m": trend(d15)
        },

        "analysis": best,
        "derivatives_context":
            funding_context(exchange, symbol),
        "updated_at": utc_now()
    }
def json_safe(x):
    if isinstance(x, dict):
        return {
            str(k): json_safe(v)
            for k, v in x.items()
        }

    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]

    if isinstance(x, np.integer):
        return int(x)

    if isinstance(x, np.floating):
        return float(x)

    if isinstance(x, np.bool_):
        return bool(x)

    if isinstance(x, float) and not math.isfinite(x):
        return None

    return x


def scan_exchange(exchange_id, cfg):
    ex = make_exchange(exchange_id)

    try:
        symbols = load_symbols(ex, cfg)

        LOG.info(
            "%s: scanning %d perpetual markets",
            exchange_id,
            len(symbols)
        )

        out = []

        for i, sym in enumerate(symbols, 1):
            try:
                frames = {}

                for tf in TIMEFRAMES:
                    frames[tf] = fetch_df(
                        ex,
                        sym,
                        tf,
                        cfg.ohlcv_limit
                    )
                    time.sleep(cfg.cooldown_seconds)

                result = analyze(
                    sym,
                    exchange_id,
                    ex,
                    frames,
                    cfg
                )

                out.append(result)

                LOG.info(
                    "%s %s %s %s score=%s",
                    exchange_id,
                    sym,
                    result["status"],
                    result["side"],
                    result["score"]
                )

            except Exception as e:
                LOG.warning(
                    "%s %s skipped: %s",
                    exchange_id,
                    sym,
                    e
                )

                out.append({
                    "exchange": exchange_id,
                    "symbol": sym,
                    "status": "DATA_ERROR",
                    "reason": str(e),
                    "updated_at": utc_now()
                })

        return out

    finally:
        try:
            ex.close()
        except Exception:
            pass


def cross_exchange_summary(results):
    buckets = {}

    for r in results:
        if r.get("status") != "SETUP":
            continue

        key = (
            r.get("symbol", "").split(":")[0],
            r.get("side")
        )

        buckets.setdefault(key, []).append(r)

    consensus = []

    for (sym, side), items in buckets.items():
        consensus.append({
            "symbol": sym,
            "side": side,
            "exchange_count": len(items),
            "exchanges": [
                x["exchange"] for x in items
            ],
            "average_score": round(
                sum(x["score"] for x in items)/len(items),
                1
            ),
            "interpretation":
                "cross-exchange agreement; still requires chart validation"
        })

    return sorted(
        consensus,
        key=lambda x: (
            x["exchange_count"],
            x["average_score"]
        ),
        reverse=True
    )


def main():
    p = argparse.ArgumentParser(
        description=(
            "Multi-exchange USDT-M Futures Signal Scanner "
            "(analysis only)"
        )
    )

    p.add_argument(
        "--exchanges",
        nargs="+",
        default=DEFAULT_EXCHANGES,
        help="CCXT ids: binanceusdm bybit okx kucoinfutures"
    )

    p.add_argument(
        "--symbols",
        nargs="*",
        help="e.g. BTC/USDT:USDT ETH/USDT:USDT"
    )

    p.add_argument("--limit", type=int, default=80)

    p.add_argument(
        "--equity",
        type=float,
        default=1000.0,
        help="equity in USDT"
    )

    p.add_argument(
        "--risk",
        type=float,
        default=0.005,
        help="risk fraction; 0.005 = 0.5 percent"
    )

    p.add_argument("--min-score", type=int, default=75)

    p.add_argument(
        "--fee",
        type=float,
        default=0.0005,
        help="estimated taker fee per side"
    )

    p.add_argument(
        "--slippage",
        type=float,
        default=0.0005,
        help="estimated slippage per side"
    )

    p.add_argument(
        "--output",
        default="signals_futures_multi.json"
    )

    p.add_argument("--log-level", default="INFO")

    a = p.parse_args()

    logging.basicConfig(
        level=getattr(
            logging,
            a.log_level.upper(),
            logging.INFO
        ),
        format="%(asctime)s %(levelname)s %(message)s"
    )

    if a.equity <= 0 or not 0 < a.risk <= 0.02:
        p.error(
            "equity must be positive; "
            "risk must be in (0, 0.02]"
        )

    if not 1 <= a.min_score <= 100:
        p.error(
            "min-score must be between 1 and 100"
        )

    if (
        not 0 <= a.fee < 0.02
        or not 0 <= a.slippage < 0.02
    ):
        p.error(
            "fee and slippage must each be in [0, 0.02)"
        )

    cfg = Config(
        exchanges=a.exchanges,
        symbols=a.symbols,
        scan_limit=max(1, a.limit),
        account_equity=a.equity,
        risk_pct=a.risk,
        min_score=a.min_score,
        fee_rate=a.fee,
        slippage_rate=a.slippage,
        output=a.output
    )

    all_results = []

    for exid in cfg.exchanges:
        try:
            all_results.extend(
                scan_exchange(exid, cfg)
            )

        except Exception as e:
            LOG.error(
                "%s exchange scan failed: %s",
                exid,
                e
            )

            all_results.append({
                "exchange": exid,
                "status": "EXCHANGE_ERROR",
                "reason": str(e),
                "updated_at": utc_now()
            })

    setups = [
        r for r in all_results
        if r.get("status") == "SETUP"
    ]

    report = {
        "scanner":
            "Signal Scanner PRO Futures Multi-Exchange",

        "generated_at": utc_now(),

        "mode": "ANALYSIS_ONLY_NO_ORDERS",

        "exchanges": cfg.exchanges,

        "summary": {
            "exchange_count": len(cfg.exchanges),
            "markets_checked": len(all_results),
            "valid_setups": len(setups),

            "no_trade": sum(
                r.get("status") == "NO_TRADE"
                for r in all_results
            ),

            "data_errors": sum(
                r.get("status") in (
                    "DATA_ERROR",
                    "EXCHANGE_ERROR"
                )
                for r in all_results
            )
        },

        "cross_exchange_consensus":
            cross_exchange_summary(all_results),

        "results": all_results,

        "disclaimer":
            "Research signals only. No order execution. "
            "Validate fees, contract sizing, funding and liquidation risk."
    }

    with open(
        cfg.output,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            json_safe(report),
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        json.dumps(
            report["summary"],
            ensure_ascii=False,
            indent=2
        )
    )

    print("Saved:", os.path.abspath(cfg.output))


if __name__ == "__main__":
    main()