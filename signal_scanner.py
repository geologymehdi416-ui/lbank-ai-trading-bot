#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py  --  V4 Pure Price-Action Engine
---------------------------------------------------
Setup (both sides, LONG is evaluated on a mirrored copy of the data):
  1. HTF bias from swing structure (4H, 1H)  -- no indicators
  2. Liquidity sweep of a confirmed swing high/low on 15m (close back inside)
  3. MSS (close beyond the last short-term swing) with displacement
  4. Limit entry on the unmitigated FVG (CE) or OB created by that leg
  5. SL beyond the sweep extreme (+ small ATR buffer, ATR is only a buffer)
  6. Targets = unbroken swing liquidity on 15m/1H/4H (no fixed-R targets)
  7. Score 0..100 with NO free points; order lifecycle tracker in logs/
Analysis and signals only. No order execution.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("signal_scanner")

LBANK_BASE = os.getenv("LBANK_BASE_URL", "https://api.lbkex.com").rstrip("/")
DEFAULT_SYMBOLS = ("BTC_USDT,ETH_USDT,SOL_USDT,TRX_USDT,XMR_USDT,"
                   "NEAR_USDT,ATOM_USDT,BCH_USDT,DOGE_USDT,LINK_USDT,FIL_USDT")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 70.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)            # % of equity risked per trade
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
MAX_STOP_PCT = _env_float("MAX_STOP_PCT", 3.0)    # max stop distance in %
FEE_PCT = _env_float("FEE_PCT", 0.06)             # per side
MIN_FINAL_RR = _env_float("MIN_FINAL_RR", 2.0)    # net RR of the last target
DISP_MIN = _env_float("DISP_MIN", 0.9)            # displacement body / ATR
SL_BUFFER_ATR = _env_float("SL_BUFFER_ATR", 0.25)
MAX_ENTRY_DIST_ATR = _env_float("MAX_ENTRY_DIST_ATR", 2.5)
PD_MIN = _env_float("PD_MIN", 0.4)                # min premium/discount position
VOL_MIN_PCT = _env_float("VOL_MIN_PCT", 0.25)     # ATR percentile floor
SWEEP_LOOKBACK = int(_env_float("SWEEP_LOOKBACK", 48))
MSS_MAX_BARS = int(_env_float("MSS_MAX_BARS", 16))
SIGNAL_FRESH_BARS = int(_env_float("SIGNAL_FRESH_BARS", 2))
ORDER_EXPIRY_CANDLES = int(_env_float("ORDER_EXPIRY_CANDLES", 12))
MAX_DATA_AGE_MINUTES = _env_float("MAX_DATA_AGE_MINUTES", 30.0)
MIN_CANDLES = 100
SIGNALS_FILE = os.getenv("SIGNALS_FILE", "logs/signals.jsonl")
NEG_INF = float("-inf")

TF_MAP: Dict[str, Tuple[str, int]] = {
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))


class ScanError(Exception):
    """Recoverable error for a single symbol / request."""


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def http_json(url: str, params: Optional[Dict[str, Any]] = None,
              retries: int = 4, timeout: float = 15.0) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "signal-scanner/4.0", "Accept": "application/json"}
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code == 429 or e.code >= 500:
                time.sleep(min(float(2 ** attempt), 30))
                continue
            raise ScanError("Network HTTP Error: %s" % last_err)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network: %s" % (getattr(e, "reason", None) or e)
            time.sleep(min(float(2 ** attempt), 20))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON payload"
            time.sleep(min(float(2 ** attempt), 20))
    raise ScanError("Request failed after %d attempts (%s)" % (retries, last_err))


class Candles:
    def __init__(self, rows: List[Tuple[float, float, float, float, float, float]]):
        self.t = [r[0] for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] for r in rows]

    def __len__(self) -> int:
        return len(self.c)


def fetch_klines(symbol: str, tf: str, size: int = 300) -> Candles:
    typ, sec = TF_MAP[tf]
    start = int(time.time()) - size * sec
    data = http_json(LBANK_BASE + "/v2/kline.do",
                     {"symbol": symbol.lower(), "size": size, "type": typ, "time": start})
    if not isinstance(data, dict) or str(data.get("result", "")).lower() != "true":
        raise ScanError("Invalid API kline response for %s [%s]" % (symbol, tf))
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("Empty data array for %s [%s]" % (symbol, tf))
    parsed: Dict[int, Tuple[float, float, float, float, float, float]] = {}
    for r in rows_raw:
        try:
            ts = int(float(r[0]))
            if ts > 10 ** 11:
                ts //= 1000
            o, h, l, c, v = (float(x) for x in r[1:6])
        except (ValueError, TypeError, IndexError):
            continue
        if min(o, h, l, c) <= 0 or h < l or v < 0:
            continue
        parsed[ts] = (ts, o, h, l, c, v)
    rows = [parsed[k] for k in sorted(parsed)]
    if rows and rows[-1][0] + sec > time.time():
        rows.pop()                      # drop the still-forming candle
    if len(rows) < MIN_CANDLES:
        raise ScanError("Insufficient candles: got %d (required %d)" % (len(rows), MIN_CANDLES))
    return Candles(rows)


def fetch_price(symbol: str) -> Optional[float]:
    try:
        data = http_json(LBANK_BASE + "/v2/ticker/24hr.do", {"symbol": symbol.lower()})
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            p = float(items[0].get("ticker", {}).get("latest"))
            if p > 0:
                return p
    except Exception:
        pass
    return None


def fetch_pairs() -> Optional[set]:
    try:
        data = http_json(LBANK_BASE + "/v2/currencyPairs.do")
    except ScanError:
        return None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    pairs = {str(x).lower() for x in items if isinstance(x, str)}
    return pairs if "btc_usdt" in pairs else None


# --------------------------------------------------------------------------
# Price-action primitives (no oscillators)
# --------------------------------------------------------------------------
def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[float]:
    """Volatility yardstick only (buffers / displacement scale), never a signal."""
    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out: List[float] = []
    a = 0.0
    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(sum(trs[:i + 1]) / (i + 1))
        elif i == n - 1:
            a = sum(trs[:n]) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


def find_swings(h: List[float], l: List[float], n: int = 3
                ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and all(h[i] >= h[i + k] for k in range(1, n + 1)):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and all(l[i] <= l[i + k] for k in range(1, n + 1)):
            lows.append((i, l[i]))
    return highs, lows


def structure_dir(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> int:
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
        return 1
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return -1
    return 0


def structure_trend(c: List[float], highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> str:
    if not highs or not lows:
        return "RANGE"
    st = structure_dir(highs, lows)
    last = c[-1]
    if st == 1:
        return "RANGE" if last < lows[-1][1] else "UP"
    if st == -1:
        return "RANGE" if last > highs[-1][1] else "DOWN"
    return "UP" if last > highs[-1][1] else ("DOWN" if last < lows[-1][1] else "RANGE")


def tf_trend(cs: Candles, n: int) -> str:
    hi, lo = find_swings(cs.h, cs.l, n)
    return structure_trend(cs.c, hi, lo)


def mirror(cs: Candles) -> Candles:
    """Negate prices so a LONG setup looks like a SHORT setup (ATR/volume invariant)."""
    return Candles([(cs.t[i], -cs.o[i], -cs.l[i], -cs.h[i], -cs.c[i], cs.v[i]) for i in range(len(cs))])


def find_sweep(cs: Candles, a: float) -> Optional[Dict[str, Any]]:
    """Most recent sweep of a confirmed swing HIGH that closed back below it,
    plus the MSS (close below the last short-term swing low) if it happened."""
    n = len(cs)
    h, l, c = cs.h, cs.l, cs.c
    highs, lows = find_swings(h, l, 3)
    for j in range(n - 1, max(n - SWEEP_LOOKBACK, 10), -1):
        cands = [(i, p) for i, p in highs
                 if j - 90 <= i < j - 2 and p < h[j] and max(h[i + 1:j]) <= p]
        if not cands:
            continue
        i, p = max(cands, key=lambda x: x[1])
        back = None
        for b in range(j, min(j + 3, n)):
            if c[b] < p:
                back = b
                break
        if back is None:
            continue
        ext = max(h[j:back + 1])
        if back + 1 < n and max(h[back + 1:]) > ext:
            continue                      # sweep high was retaken
        ref_lows = [v for q, v in lows if i < q < j]
        ref = ref_lows[-1] if ref_lows else min(l[i:j])
        k = None
        for x in range(j, n):
            if c[x] < ref:
                k = x
                break
        eq = sum(1 for q, v in highs if q != i and q < j and abs(v - p) <= 0.15 * a)
        return {"i": i, "p": p, "j": j, "back": back, "ext": ext, "ref": ref, "k": k,
                "eq": eq, "wick": (h[j] - p) / a}
    return None


def find_zone(cs: Candles, j: int, k: int, a: float) -> Optional[Tuple[float, float, float, str]]:
    """Unmitigated bearish FVG (preferred) or OB left by the MSS leg. -> (lo, hi, entry, kind)"""
    n = len(cs)
    h, l, o, c = cs.h, cs.l, cs.o, cs.c
    best = None
    for m in range(max(1, j - 1), min(k, n - 3) + 1):
        if h[m + 2] < l[m] and (l[m] - h[m + 2]) >= 0.1 * a:
            lo, hi = h[m + 2], l[m]
            ce = (lo + hi) / 2.0
            if max(h[m + 3:], default=NEG_INF) < ce and (best is None or ce > best[2]):
                best = (lo, hi, ce, "FVG")
    if best:
        return best
    for q in range(k - 1, max(j - 6, 0), -1):
        if c[q] > o[q]:
            ce = (o[q] + c[q]) / 2.0
            if max(h[q + 1:], default=NEG_INF) < ce:
                return (l[q], h[q], ce, "OB")
            return None
    return None


def liquidity_below(M: Dict[str, Candles], entry: float, a: float) -> List[float]:
    """Unbroken swing lows (15m/1H/4H) below entry, nearest first, clustered."""
    pools: List[float] = []
    for tf, n in (("15m", 3), ("1h", 3), ("4h", 2)):
        cs = M[tf]
        _, lows = find_swings(cs.h, cs.l, n)
        for idx, p in lows:
            if p < entry and min(cs.l[idx + 1:], default=p) >= p:
                pools.append(p)
    pools.sort(reverse=True)
    merged: List[float] = []
    for p in pools:
        if not merged or merged[-1] - p > 0.3 * a:
            merged.append(p)
    return merged


# --------------------------------------------------------------------------
# Core evaluation (always "SHORT" in the space it is given)
# --------------------------------------------------------------------------
def core_short(M: Dict[str, Candles], price: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "NONE", "score": 0, "parts": {}, "notes": [],
                           "blockers": [], "plan": None}
    c15, c1, c4 = M["15m"], M["1h"], M["4h"]
    n = len(c15)
    at = atr(c15.h, c15.l, c15.c)
    a = max(at[-1], abs(c15.c[-1]) * 1e-6)

    t4, t1 = tf_trend(c4, 2), tf_trend(c1, 3)
    if t4 == "UP" or (t4 == "RANGE" and t1 != "DOWN"):
        out["blockers"].append("ساختار 4H/1H هم‌جهت نیست")
        return out
    out["notes"].append("ساختار HTF هم‌جهت (4H: %s | 1H: %s)" % (t4, t1))

    sw = find_sweep(c15, a)
    if sw is None:
        out["blockers"].append("سوییپ نقدینگی معتبر در بازه‌ی اخیر نیست")
        return out
    j, k = sw["j"], sw["k"]
    out["sweep_ts"] = int(c15.t[j])
    out["sweep_level"] = sw["p"]
    if price > sw["ext"]:
        out["blockers"].append("قیمت بالاتر از سقف سوییپ رفت (ایده باطل)")
        return out
    if k is None:
        out["status"] = "WATCH"
        out["notes"].append("سوییپ انجام شد؛ منتظر MSS (کلوز زیر %.8g)" % sw["ref"])
        return out
    if n - 1 - k > MSS_MAX_BARS:
        out["blockers"].append("ستاپ قدیمی است")
        return out

    disp = max(((c15.o[x] - c15.c[x]) / max(at[x], 1e-12) for x in range(j, min(k + 2, n))), default=0.0)
    if disp < DISP_MIN:
        out["status"] = "WATCH"
        out["notes"].append("MSS بدون دیسپلیسمنت کافی (%.2f ATR)" % disp)
        return out

    zone = find_zone(c15, j, k, a)
    if zone is None:
        out["status"] = "WATCH"
        out["notes"].append("ناحیه‌ی ورود دست‌نخورده (FVG/OB) وجود ندارد")
        return out
    zlo, zhi, entry, kind = zone
    if entry <= price:
        out["status"] = "WATCH"
        out["notes"].append("قیمت از ناحیه‌ی ورود عبور کرده/داخل آن است")
        return out
    if (entry - price) / a > MAX_ENTRY_DIST_ATR:
        out["status"] = "WATCH"
        out["notes"].append("ناحیه‌ی ورود از قیمت دور است (%.1f ATR)" % ((entry - price) / a))
        return out

    sl = sw["ext"] + SL_BUFFER_ATR * a
    risk = sl - entry
    risk_pct = risk / abs(entry) * 100.0
    if risk < 0.5 * a:
        out["blockers"].append("حد ضرر برای نویز خیلی تنگ است")
    if risk_pct > MAX_STOP_PCT:
        out["blockers"].append("حد ضرر ساختاری بزرگ است (%.2f%%)" % risk_pct)

    hi4, lo4 = max(c4.h[-60:]), min(c4.l[-60:])
    pd_pos = (entry - lo4) / (hi4 - lo4) if hi4 > lo4 else 0.5
    if pd_pos < PD_MIN:
        out["blockers"].append("ورود در ناحیه‌ی Discount بازه‌ی 4H است")

    window = at[-200:]
    pct = sum(1 for x in window if x <= at[-1]) / float(len(window))
    if pct < VOL_MIN_PCT and disp < 1.5:
        out["blockers"].append("بازار کم‌نوسان و بدون بریک‌اوت قوی")

    tps: List[float] = []
    if risk > 0:
        tps = [p for p in liquidity_below(M, entry, a) if 1.0 <= (entry - p) / risk <= 8.0][:3]
    fee = abs(entry) * 2.0 * FEE_PCT / 100.0
    final_rr = ((entry - tps[-1] - fee) / risk) if (tps and risk > 0) else 0.0
    if not tps:
        out["blockers"].append("نقدینگی هدف‌دار (سوینگ‌شکسته‌نشده) با RR≥1 وجود ندارد")
    elif final_rr < MIN_FINAL_RR:
        out["blockers"].append("RR خالص آخرین هدف کمتر از %.1f است (%.2f)" % (MIN_FINAL_RR, final_rr))

    base = max(sum(c15.v[max(0, j - 30):j]) / max(1, len(c15.v[max(0, j - 30):j])), 1e-12)
    vr = (sum(c15.v[j:k + 1]) / float(k + 1 - j)) / base
    parts = {
        "htf": (15.0 if t4 == "DOWN" else 5.0) + (10.0 if t1 == "DOWN" else 0.0),
        "sweep": 10.0 + (6.0 if sw["eq"] >= 1 else 0.0) + (4.0 if sw["wick"] >= 0.3 else 0.0),
        "displacement": min(15.0, round(10.0 * disp, 1)),
        "zone": (10.0 if kind == "FVG" else 6.0) + (5.0 if pd_pos >= 0.75 else (3.0 if pd_pos >= PD_MIN else 0.0)),
        "rr": min(15.0, max(0.0, final_rr * 5.0)),
        "volatility_volume": (5.0 if pct >= 0.4 else (3.0 if pct >= VOL_MIN_PCT else 0.0))
                             + (5.0 if vr >= 1.2 else (2.0 if vr >= 1.0 else 0.0)),
    }
    score = int(round(sum(parts.values())))
    out["parts"], out["score"] = parts, score

    out["notes"].append("سوییپ %s%s سقف سوینگ → MSS با دیسپلیسمنت %.2f ATR"
                        % ("Equal Highs و " if sw["eq"] >= 1 else "", "", disp))
    out["notes"].append("ورود روی %s | موقعیت در بازه‌ی 4H: %.0f%%" % (kind, pd_pos * 100))

    fresh = (n - 1 - k) <= SIGNAL_FRESH_BARS
    if out["blockers"]:
        return out
    if not fresh:
        out["status"] = "WATCH"
        out["notes"].append("MSS قدیمی‌تر از %d کندل؛ سیگنال قبلاً صادر شده/فرصت گذشته" % SIGNAL_FRESH_BARS)
        return out
    out["status"] = "ENTER" if score >= MIN_SCORE else "WATCH"
    if out["status"] == "WATCH":
        out["notes"].append("امتیاز %d کمتر از حداقل %d" % (score, MIN_SCORE))
    out["plan"] = {"entry": entry, "sl": sl, "tps": tps, "risk_pct": risk_pct, "kind": kind,
                   "zone": (zlo, zhi), "ext": sw["ext"], "rr": [(entry - p) / risk for p in tps],
                   "net_rr_final": final_rr}
    return out


def _neg_plan(plan: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not plan:
        return plan
    q = dict(plan)
    q["entry"], q["sl"], q["ext"] = -plan["entry"], -plan["sl"], -plan["ext"]
    q["tps"] = [-p for p in plan["tps"]]
    q["zone"] = (-plan["zone"][1], -plan["zone"][0])
    return q


def analyze_candles(symbol: str, C: Dict[str, Candles], price: float, min_score: float) -> Dict[str, Any]:
    stale_min = (time.time() - (C["15m"].t[-1] + TF_MAP["15m"][1])) / 60.0
    short = core_short(C, price)
    short["side"] = "SHORT"
    M = {tf: mirror(C[tf]) for tf in ("15m", "1h", "4h")}
    long_ = core_short(M, -price)
    long_["side"] = "LONG"
    long_["plan"] = _neg_plan(long_["plan"])
    if "sweep_level" in long_:
        long_["sweep_level"] = -long_["sweep_level"]
    rank = {"ENTER": 2, "WATCH": 1, "NONE": 0}
    best = max((short, long_), key=lambda r: (rank[r["status"]], r["score"]))
    decision = "ENTER" if best["status"] == "ENTER" else "NO_ENTRY"
    if decision == "ENTER" and stale_min > MAX_DATA_AGE_MINUTES:
        decision = "NO_ENTRY"
        best["status"] = "WATCH"
        best["notes"].append("داده‌ی بازار کهنه است (%.0f دقیقه)" % stale_min)
    return {"symbol": symbol, "price": price, "best_sc": best, "decision": decision,
            "data_age_min": stale_min, "scenarios": {"SHORT": short, "LONG": long_}}


def analyze_symbol(symbol: str, min_score: float) -> Dict[str, Any]:
    C = {tf: fetch_klines(symbol, tf) for tf in ("4h", "1h", "15m")}
    price = fetch_price(symbol) or C["15m"].c[-1]
    return analyze_candles(symbol, C, price, min_score)


# --------------------------------------------------------------------------
# Signal registry & order lifecycle (needs logs/ to persist between runs)
# --------------------------------------------------------------------------
def load_signals() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def save_signals(recs: List[Dict[str, Any]]) -> None:
    d = os.path.dirname(SIGNALS_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def next_trade_id(recs: List[Dict[str, Any]]) -> str:
    nums = [int(str(r["id"])[3:]) for r in recs
            if str(r.get("id", "")).startswith("TR-") and str(r["id"])[3:].isdigit()]
    return "TR-%06d" % ((max(nums) if nums else 0) + 1)


def advance_signal(rec: Dict[str, Any], cs: Candles) -> None:
    """Replay closed 15m candles after the last processed one (conservative: SL first)."""
    short = rec["side"] == "SHORT"
    entry, sl, tps = rec["entry"], rec["sl"], rec["tps"]
    risk = abs(sl - entry)
    for i in range(len(cs)):
        if rec["status"] not in ("PENDING", "OPEN"):
            break
        if cs.t[i] <= rec.get("last_ts", 0) or cs.t[i] < rec["created_ts"]:
            continue
        hi, lo = cs.h[i], cs.l[i]
        rec["last_ts"] = cs.t[i]
        if rec["status"] == "PENDING":
            rec["bars"] = rec.get("bars", 0) + 1
            if (short and hi >= entry) or (not short and lo <= entry):
                rec["status"] = "OPEN"
                rec["tp_hit"] = 0
            elif (short and lo <= tps[0]) or (not short and hi >= tps[0]):
                rec["status"], rec["outcome"] = "CANCELLED", "TP1_BEFORE_FILL"
                continue
            elif rec["bars"] >= ORDER_EXPIRY_CANDLES:
                rec["status"], rec["outcome"] = "EXPIRED", "NOT_FILLED"
                continue
            else:
                continue
        fav = (entry - lo) / risk if short else (hi - entry) / risk
        rec["mfe_r"] = round(max(rec.get("mfe_r", 0.0), fav), 2)
        if (short and hi >= sl) or (not short and lo <= sl):
            rec["status"] = "CLOSED"
            rec["outcome"] = "SL" if rec["tp_hit"] == 0 else "SL_AFTER_TP%d" % rec["tp_hit"]
            continue
        while rec["tp_hit"] < len(tps):
            tp = tps[rec["tp_hit"]]
            if (short and lo <= tp) or (not short and hi >= tp):
                rec["tp_hit"] += 1
            else:
                break
        if rec["tp_hit"] >= len(tps):
            rec["status"], rec["outcome"] = "CLOSED", "TP%d" % rec["tp_hit"]


def print_stats(recs: List[Dict[str, Any]]) -> None:
    print("signals: %d | pending: %d | open: %d" % (
        len(recs), sum(r["status"] == "PENDING" for r in recs), sum(r["status"] == "OPEN" for r in recs)))
    for lo_s, hi_s in ((0, 79), (80, 89), (90, 100)):
        b = [r for r in recs if lo_s <= r.get("score", 0) <= hi_s]
        filled = [r for r in b if r.get("outcome") not in ("NOT_FILLED", "TP1_BEFORE_FILL") and r["status"] != "PENDING"]
        wins = [r for r in filled if str(r.get("outcome", "")).startswith("TP")
                or str(r.get("outcome", "")).startswith("SL_AFTER")]
        sls = [r for r in filled if r.get("outcome") == "SL"]
        print("score %3d-%3d: n=%d filled/closed=%d  TP-reached=%d  SL=%d" % (
            lo_s, hi_s, len(b), len(filled), len(wins), len(sls)))


# --------------------------------------------------------------------------
# Telegram report
# --------------------------------------------------------------------------
def fmt_price(p: float) -> str:
    if p >= 1000:
        return "%.2f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p


def format_brief(res: Dict[str, Any], trade_id: str) -> List[str]:
    sc = res["best_sc"]
    pl = sc["plan"]
    short = sc["side"] == "SHORT"
    notional = RISK_PCT / pl["risk_pct"]                 # position value / equity
    notional = min(notional, MAX_LEVERAGE)
    lev = max(1.0, notional)
    nums = ["1️⃣", "2️⃣", "3️⃣"]
    lines = [
        "⏳ سفارش لیمیت در انتظار رسیدن قیمت", "",
        "شناسه معامله: %s" % trade_id,
        "نماد: %s" % res["symbol"].replace("_", ""),
        "جهت: %s" % ("شورت (فروش)" if short else "لانگ (خرید)"),
        "ورود لیمیت: %s" % fmt_price(pl["entry"]),
        "اهرم: %.1fx | حجم پوزیشن ≈ %.0f%% سرمایه (ریسک %.2g%%)" % (lev, notional * 100.0, RISK_PCT),
        "اهداف: " + " ".join("%s %s" % (nums[i], fmt_price(p)) for i, p in enumerate(pl["tps"])),
        "حد ضرر: %s" % fmt_price(pl["sl"]),
        "RR هدف اول/آخر: %.1f / %.1f (خالص آخر: %.2f)" % (pl["rr"][0], pl["rr"][-1], pl["net_rr_final"]),
        "",
        "📊 تحلیل پرایس‌اکشن:",
        "• امتیاز کیفیت ستاپ: %d/100" % sc["score"],
    ]
    for note in sc["notes"]:
        lines.append("• %s" % note)
    lines += [
        "❎ لغو سفارش اگر: قیمت قبل از پر شدن به هدف اول برسد، یا کلوز %s %s، یا بعد از %d کندل 15m پر نشود."
        % ("بالای" if short else "پایین", fmt_price(pl["ext"]), ORDER_EXPIRY_CANDLES),
    ]
    return lines


def format_watch(res: Dict[str, Any]) -> str:
    sc = res["best_sc"]
    why = sc["notes"][-1] if sc["notes"] else "؟"
    return "👀 %s %s — %s" % (res["symbol"].replace("_", ""), "شورت" if sc["side"] == "SHORT" else "لانگ", why)


def format_report(entries: List[Tuple[Dict[str, Any], str]], watches: List[Dict[str, Any]],
                  errors: List[Tuple[str, str]]) -> str:
    now = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M (به وقت ایران)")
    lines: List[str] = []
    for res, tid in entries:
        lines.extend(format_brief(res, tid))
        lines.append("-" * 25)
    if not entries:
        lines.append("⏳ اسکن بازار — %s" % now)
        lines.append("سیگنال تأییدشده‌ای در این چرخه نیست.")
    if watches:
        lines += ["", "📋 واچ‌لیست (سیگنال نیست؛ فقط پیگیری):"]
        lines += [format_watch(w) for w in watches[:6]]
    for sym, err in errors:
        lines.append("❗ %s: %s" % (sym, err[:100]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-track", action="store_true", help="skip signal registry/lifecycle")
    ap.add_argument("--stats", action="store_true", help="print registry stats and exit")
    args = ap.parse_args(argv)

    if args.stats:
        print_stats(load_signals())
        return 0

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = fetch_pairs()
    if pairs:
        symbols = [x for x in symbols if x.lower() in pairs]

    track = not args.no_track
    recs = load_signals() if track else []
    errors: List[Tuple[str, str]] = []

    if track:
        for sym in {r["symbol"] for r in recs if r["status"] in ("PENDING", "OPEN")}:
            try:
                cs = fetch_klines(sym, "15m")
                for r in recs:
                    if r["symbol"] == sym:
                        advance_signal(r, cs)
            except ScanError as e:
                errors.append((sym, "update: %s" % e))

    entries: List[Tuple[Dict[str, Any], str]] = []
    watches: List[Dict[str, Any]] = []
    for sym in symbols:
        try:
            res = analyze_symbol(sym, args.min_score)
        except ScanError as e:
            errors.append((sym, str(e)))
            continue
        except Exception as e:  # keep the scan alive
            errors.append((sym, "خطای سیستمی: %s" % e))
            continue
        sc = res["best_sc"]
        if res["decision"] == "ENTER":
            dup = any(r["symbol"] == sym and r["side"] == sc["side"] and r.get("sweep_ts") == sc.get("sweep_ts")
                      for r in recs)
            if dup:
                continue
            tid = next_trade_id(recs)
            now = int(time.time())
            pl = sc["plan"]
            recs.append({"id": tid, "symbol": sym, "side": sc["side"], "status": "PENDING",
                         "created_ts": now, "last_ts": now - 1, "created_iso": datetime.now(IRAN_TZ).isoformat(),
                         "score": sc["score"], "parts": sc["parts"], "entry": pl["entry"], "sl": pl["sl"],
                         "tps": pl["tps"], "sweep_ts": sc.get("sweep_ts"), "bars": 0})
            entries.append((res, tid))
        elif sc["status"] == "WATCH":
            watches.append(res)

    report_text = format_report(entries, watches, errors)
    path = os.getenv("REPORT_FILE", "report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print("Successfully generated %s" % path)
    except OSError as e:
        print("Failed to write report file: %s" % e)

    if track and not args.dry_run:
        try:
            save_signals(recs)
        except OSError as e:
            print("Failed to save signals: %s" % e)
    if args.dry_run:
        print(report_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
