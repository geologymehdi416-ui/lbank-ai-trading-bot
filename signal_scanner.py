#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_scanner.py
-----------------
Analysis-only crypto futures signal scanner (NO order execution).

Environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram delivery (if missing -> stdout)
  SYMBOLS            comma list, default BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT
  MIN_SCORE          default 75
  RISK_PCT           risk per trade in % of equity, default 0.5
  MAX_LEVERAGE       default 2
  ACCOUNT_EQUITY     optional (USDT)
  LBANK_BASE_URL     default https://api.lbkex.com
  REQUEST_INTERVAL   min seconds between API calls, default 0.25
  STATE_FILE         cooldown file, default signal_state.json
  SIGNAL_COOLDOWN_H  hours before the same symbol+side is re-sent, default 4
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("signal_scanner")

LBANK_BASE = os.getenv("LBANK_BASE_URL", "https://api.lbkex.com").rstrip("/")
DEFAULT_SYMBOLS = "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,XRP_USDT"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 75.0)
RISK_PCT = _env_float("RISK_PCT", 0.5)
MAX_LEVERAGE = _env_float("MAX_LEVERAGE", 2.0)
REQUEST_INTERVAL = _env_float("REQUEST_INTERVAL", 0.25)
COOLDOWN_H = _env_float("SIGNAL_COOLDOWN_H", 4.0)
STATE_FILE = os.getenv("STATE_FILE", "signal_state.json")

MAX_STOP_PCT = 3.0
MAX_STOP_ATR = 3.0
MIN_STOP_ATR = 0.5
MIN_ATR_PCT = 0.0008
MAX_ATR_PCT = 0.03
MIN_VOL_RATIO = 0.5
MIN_CANDLES = 100

TF_MAP: Dict[str, Tuple[str, int]] = {
    "5m": ("minute5", 300),
    "15m": ("minute15", 900),
    "1h": ("hour1", 3600),
    "4h": ("hour4", 14400),
}

TREND_FA = {"UP": "صعودی", "DOWN": "نزولی", "RANGE": "رنج"}


class ScanError(Exception):
    """Recoverable error for a single symbol / request."""


_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def http_json(url: str, params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None,
              retries: int = 4, timeout: float = 15.0) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"User-Agent": "signal-scanner/1.0", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code == 429 or e.code >= 500:
                delay = float(2 ** attempt)
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra:
                    try:
                        delay = max(delay, float(ra))
                    except ValueError:
                        pass
                log.warning("%s - retry %d/%d in %.0fs", last_err, attempt, retries, min(delay, 30))
                time.sleep(min(delay, 30))
                continue
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            raise ScanError("%s %s" % (last_err, detail))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = "network: %s" % (getattr(e, "reason", None) or e)
            log.warning("%s - retry %d/%d", last_err, attempt, retries)
            time.sleep(min(2 ** attempt, 20))
        except (json.JSONDecodeError, ValueError):
            last_err = "invalid JSON response"
            log.warning("%s - retry %d/%d", last_err, attempt, retries)
            time.sleep(min(2 ** attempt, 20))
    raise ScanError("request failed after %d tries (%s)" % (retries, last_err))
class Candles:
    """Column-oriented OHLCV container (closed candles only)."""

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
    now = int(time.time())
    start = now - size * sec
    data = http_json(LBANK_BASE + "/v2/kline.do",
                     {"symbol": symbol.lower(), "size": size, "type": typ, "time": start})
    if not isinstance(data, dict) or str(data.get("result", "")).lower() != "true":
        raise ScanError("kline %s %s: bad response %s" % (symbol, tf, str(data)[:120]))
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        raise ScanError("kline %s %s: no data" % (symbol, tf))
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
        rows.pop()
    if len(rows) < MIN_CANDLES:
        raise ScanError("kline %s %s: only %d candles" % (symbol, tf, len(rows)))
    age = time.time() - (rows[-1][0] + sec)
    if age > 2 * sec + 120:
        raise ScanError("kline %s %s: stale data (%.0f min old)" % (symbol, tf, age / 60))
    return Candles(rows)


def fetch_price(symbol: str) -> Optional[float]:
    try:
        data = http_json(LBANK_BASE + "/v2/ticker/24hr.do", {"symbol": symbol.lower()})
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            tk = items[0].get("ticker", {})
            p = float(tk.get("latest"))
            if p > 0:
                return p
    except (ScanError, TypeError, ValueError, AttributeError) as e:
        log.warning("ticker %s failed: %s", symbol, e)
    return None


def ema(values: List[float], n: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def _rsi_val(ag: float, al: float) -> float:
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def rsi(closes: List[float], n: int = 14) -> List[float]:
    if len(closes) <= n:
        return [50.0] * len(closes)
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out = [50.0] * n
    out.append(_rsi_val(ag, al))
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out.append(_rsi_val(ag, al))
    return out


def macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9
         ) -> Tuple[List[float], List[float], List[float]]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    signal = ema(line, sig)
    hist = [a - b for a, b in zip(line, signal)]
    return line, signal, hist


def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[float]:
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


def volume_ratio(v: List[float]) -> float:
    if len(v) < 24:
        return 1.0
    recent = sum(v[-3:]) / 3.0
    base = sum(v[-23:-3]) / 20.0
    if base <= 0:
        return 1.0
    return recent / base
def find_swings(h: List[float], l: List[float], n: int = 3
                ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - k] for k in range(1, n + 1)) and \
           all(h[i] >= h[i + k] for k in range(1, n + 1)):
            highs.append((i, h[i]))
        if all(l[i] < l[i - k] for k in range(1, n + 1)) and \
           all(l[i] <= l[i + k] for k in range(1, n + 1)):
            lows.append((i, l[i]))
    return highs, lows


def structure_dir(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> int:
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


def build_tf(cs: Candles, name: str) -> Dict[str, Any]:
    c, h, l = cs.c, cs.h, cs.l
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    rs = rsi(c)
    ml, ms, mh = macd(c)
    at = atr(h, l, c)
    highs, lows = find_swings(h, l, 2 if name == "4h" else 3)
    st = structure_dir(highs, lows)
    pts = 0
    pts += 1 if c[-1] > e50[-1] else -1
    pts += 1 if e20[-1] > e50[-1] else -1
    pts += 1 if c[-1] > e200[-1] else -1
    pts += 2 * st
    trend = "UP" if pts >= 3 else "DOWN" if pts <= -3 else "RANGE"
    return {
        "name": name, "cs": cs, "close": c[-1],
        "ema20": e20[-1], "ema50": e50[-1], "ema200": e200[-1],
        "rsi": rs[-1], "macd": ml[-1], "macd_sig": ms[-1],
        "hist": mh[-1], "hist_prev": mh[-2] if len(mh) > 1 else mh[-1],
        "atr": max(at[-1], c[-1] * 1e-6), "vol_ratio": volume_ratio(cs.v),
        "highs": highs, "lows": lows, "struct": st, "trend": trend,
        "trend_pts": pts,
    }


def detect_sweep(d: Dict[str, Any], side: str, lookback: int) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    a = d["atr"]
    long = side == "LONG"
    pts = [p for p in (d["lows"] if long else d["highs"]) if p[0] >= n - 150]
    for j in range(n - 1, max(n - 1 - lookback, 2), -1):
        for si, lvl in reversed(pts):
            if si >= j - 1:
                continue
            if long:
                ok = cs.l[j] < lvl - 0.03 * a and cs.c[j] > lvl and \
                    all(x >= lvl for x in cs.c[si + 1:j])
                ext = min(cs.l[j:]) if ok else 0.0
            else:
                ok = cs.h[j] > lvl + 0.03 * a and cs.c[j] < lvl and \
                    all(x <= lvl for x in cs.c[si + 1:j])
                ext = max(cs.h[j:]) if ok else 0.0
            if ok:
                return {"idx": j, "level": lvl, "extreme": ext, "age": n - 1 - j, "tf": d["name"]}
    return None


def detect_mss(d: Dict[str, Any], side: str, sweep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    j = sweep["idx"]
    if side == "LONG":
        prior = [p for p in d["highs"] if p[0] < j]
        if not prior:
            return None
        lvl = prior[-1][1]
        for k in range(j, len(cs)):
            if cs.c[k] > lvl:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    else:
        prior = [p for p in d["lows"] if p[0] < j]
        if not prior:
            return None
        lvl = prior[-1][1]
        for k in range(j, len(cs)):
            if cs.c[k] < lvl:
                return {"idx": k, "level": lvl, "tf": d["name"]}
    return None


def detect_bos(d: Dict[str, Any], side: str, within: int = 6) -> Optional[Dict[str, Any]]:
    cs: Candles = d["cs"]
    n = len(cs)
    if side == "LONG":
        prior = [p for p in d["highs"] if p[0] < n - within]
        if prior and max(cs.c[-within:]) > prior[-1][1]:
            return {"level": prior[-1][1], "tf": d["name"]}
    else:
        prior = [p for p in d["lows"] if p[0] < n - within]
        if prior and min(cs.c[-within:]) < prior[-1][1]:
            return {"level": prior[-1][1], "tf": d["name"]}
    return None


def find_fvgs(cs: Candles, atr_v: float, side: str, lookback: int = 50) -> List[Dict[str, float]]:
    n = len(cs)
    out: List[Dict[str, float]] = []
    for i in range(max(2, n - lookback), n):
        if side == "LONG":
            lo, hi = cs.h[i - 2], cs.l[i]
            if hi > lo and hi - lo >= 0.1 * atr_v:
                later = min(cs.l[i + 1:]) if i + 1 < n else float("inf")
                if later > lo:
                    out.append({"bottom": lo, "top": hi})
        else:
            hi, lo = cs.l[i - 2], cs.h[i]
            if hi > lo and hi - lo >= 0.1 * atr_v:
                later = max(cs.h[i + 1:]) if i + 1 < n else float("-inf")
                if later < hi:
                    out.append({"bottom": lo, "top": hi})
    return out


def find_obs(cs: Candles, atr_v: float, side: str, lookback: int = 60) -> List[Dict[str, float]]:
    n = len(cs)
    out: List[Dict[str, float]] = []
    for i in range(max(1, n - lookback), n - 3):
        if side == "LONG" and cs.c[i] < cs.o[i]:
            move = max(cs.h[i + 1:i + 4]) - cs.h[i]
            if move >= 0.8 * atr_v and min(cs.c[i + 1:]) > cs.l[i]:
                out.append({"bottom": cs.l[i], "top": cs.o[i]})
        elif side == "SHORT" and cs.c[i] > cs.o[i]:
            move = cs.l[i] - min(cs.l[i + 1:i + 4])
            if move >= 0.8 * atr_v and max(cs.c[i + 1:]) < cs.h[i]:
                out.append({"bottom": cs.o[i], "top": cs.h[i]})
    return out


def _zones_for(d: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    zs: List[Dict[str, Any]] = []
    for z in find_fvgs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "FVG", "tf": d["name"]})
    for z in find_obs(d["cs"], d["atr"], side):
        zs.append({"bottom": z["bottom"], "top": z["top"], "kind": "OB", "tf": d["name"]})
    return zs


def pick_zone(ds: List[Dict[str, Any]], side: str, price: float, atr15: float
              ) -> Optional[Dict[str, Any]]:
    long = side == "LONG"
    allz: List[Dict[str, Any]] = []
    for d in ds:
        allz.extend(_zones_for(d, side))

    def dist(z: Dict[str, Any]) -> float:
        return max(0.0, price - z["top"]) if long else max(0.0, z["bottom"] - price)

    def valid(z: Dict[str, Any]) -> bool:
        if z["top"] <= z["bottom"]:
            return False
        if long:
            return z["top"] <= price + 0.1 * atr15 and z["bottom"] < price and dist(z) <= 1.5 * atr15
        return z["bottom"] >= price - 0.1 * atr15 and z["top"] > price and dist(z) <= 1.5 * atr15

    cands: List[Tuple[int, float, Dict[str, Any]]] = []
    fvgs = [z for z in allz if z["kind"] == "FVG"]
    obs = [z for z in allz if z["kind"] == "OB"]
    for f in fvgs:
        for o in obs:
            lo, hi = max(f["bottom"], o["bottom"]), min(f["top"], o["top"])
            if hi > lo:
                z = {"bottom": lo, "top": hi, "kind": "OB+FVG", "tf": f["tf"]}
                if valid(z):
                    cands.append((0, dist(z), z))
    for z in allz:
        if valid(z):
            cands.append((1, dist(z), z))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def collect_levels(D: Dict[str, Dict[str, Any]], htf_only: bool = False) -> List[float]:
    names = ("1h", "4h") if htf_only else ("15m", "1h", "4h")
    lv: List[float] = []
    for nm in names:
        d = D[nm]
        lv.extend(p for _, p in d["highs"])
        lv.extend(p for _, p in d["lows"])
    return sorted(set(lv))


def liquidity_map(d: Dict[str, Any], price: float) -> Dict[str, Any]:
    tol = 0.15 * d["atr"]
    above = [p for _, p in d["highs"] if p > price]
    below = [p for _, p in d["lows"] if p < price]
    out: Dict[str, Any] = {"bsl": None, "bsl_eq": False, "ssl": None, "ssl_eq": False}
    if above:
        b = min(above)
        out["bsl"] = b
        out["bsl_eq"] = sum(1 for _, p in d["highs"] if abs(p - b) <= tol) >= 2
    if below:
        s = max(below)
        out["ssl"] = s
        out["ssl_eq"] = sum(1 for _, p in d["lows"] if abs(p - s) <= tol) >= 2
    return out
def fmt_price(p: float) -> str:
    if p >= 1000:
        return "%.2f" % p
    if p >= 1:
        return "%.4f" % p
    if p >= 0.01:
        return "%.5f" % p
    return "%.8f" % p


def format_signal(res: Dict[str, Any], sc: Dict[str, Any]) -> str:
    t = sc["trade"]
    long = sc["side"] == "LONG"
    icon = "🟢" if long else "🔴"
    lines = [
        "%s <b>سیگنال %s — %s</b>" % (icon, sc["side"], html.escape(res["symbol"])),
        "امتیاز: <b>%d/100</b>" % sc["score"],
        "",
        "ورود (%s): <code>%s</code>" % (t["order_type"], fmt_price(t["entry"])),
        "حد ضرر: <code>%s</code> (%.2f%%)" % (fmt_price(t["sl"]), t["stop_pct"]),
        "TP1: <code>%s</code> (RR %.2f)" % (fmt_price(t["tp1"]), t["rr1"]),
        "TP2: <code>%s</code> (RR %.2f)" % (fmt_price(t["tp2"]), t["rr2"]),
        "",
        "ریسک پیشنهادی: %.2f%% سرمایه | حداکثر اهرم: %.0fx" % (RISK_PCT, MAX_LEVERAGE),
        "اهرم مؤثر پیشنهادی: %.2fx" % t["notional_x"],
    ]
    if t["capped"]:
        lines.append("⚠️ به‌دلیل سقف اهرم، ریسک مؤثر ≈ %.2f%% است" % t["eff_risk_pct"])
    eq = _env_float("ACCOUNT_EQUITY", 0.0)
    if eq > 0:
        lines.append("حجم پوزیشن (تقریبی): %.2f USDT" % (eq * t["notional_x"]))
    lines.append("")
    lines.append("دلایل:")
    for n in sc["notes"]:
        lines.append("• " + html.escape(n))
    lines.append("")
    lines.append("قیمت فعلی: %s" % fmt_price(res["price"]))
    lines.append("اگر قبل از فعال‌شدن ورود، حد ضرر لمس شود سیگنال باطل است.")
    lines.append("⚠️ فقط تحلیل است؛ سفارشی به‌صورت خودکار ثبت نمی‌شود.")
    return "\n".join(lines)


def format_report(results: List[Dict[str, Any]], errors: List[Tuple[str, str]],
                  sent_signals: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["📊 <b>گزارش اسکن</b> — %s" % now, ""]
    for res in results:
        sym = html.escape(res["symbol"])
        D = res["D"]
        lg, sh = res["scenarios"]["LONG"], res["scenarios"]["SHORT"]
        lines.append("<b>%s</b> | قیمت: %s" % (sym, fmt_price(res["price"])))
        lines.append("روند 4H/1H/15m: %s / %s / %s" % (
            TREND_FA[D["4h"]["trend"]], TREND_FA[D["1h"]["trend"]], TREND_FA[D["15m"]["trend"]]))
        lines.append("LONG: %d | SHORT: %d" % (lg["score"], sh["score"]))
        sig = res["signal"]
        if sig:
            lines.append("✅ سیگنال %s صادر شد" % sig["side"])
        else:
            best = lg if lg["score"] >= sh["score"] else sh
            if res["global_blockers"]:
                reason = res["global_blockers"][0]
            elif best["blockers"]:
                reason = best["blockers"][0]
            else:
                reason = "امتیاز %d کمتر از حداقل %d است" % (best["score"], MIN_SCORE)
            lines.append("⛔ بدون سیگنال: %s" % html.escape(reason))
            bt = best["trade"]
            if bt:
                lines.append("سناریوی %s (غیرفعال): ورود %s | SL %s | TP1 %s | TP2 %s" % (
                    best["side"], fmt_price(bt["entry"]), fmt_price(bt["sl"]),
                    fmt_price(bt["tp1"]), fmt_price(bt["tp2"])))
        liq = res["liq"]
        pool = []
        if liq["bsl"] is not None:
            pool.append("BSL%s %s" % ("(EQH)" if liq["bsl_eq"] else "", fmt_price(liq["bsl"])))
        if liq["ssl"] is not None:
            pool.append("SSL%s %s" % ("(EQL)" if liq["ssl_eq"] else "", fmt_price(liq["ssl"])))
        if pool:
            lines.append("نقدینگی نزدیک: " + " | ".join(pool))
        lines.append("")
    for sym, err in errors:
        lines.append("❗ %s: %s" % (html.escape(sym), html.escape(err[:150])))
    lines.append("تعداد سیگنال ارسال‌شده: %d | حداقل امتیاز: %d" % (sent_signals, MIN_SCORE))
    return "\n".join(lines)


def split_message(text: str, limit: int = 3800) -> List[str]:
    chunks: List[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead")
        print(text)
        return False
    ok = True
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    for chunk in split_message(text):
        try:
            resp = http_json(url, body={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                        "disable_web_page_preview": True})
            if not isinstance(resp, dict) or not resp.get("ok"):
                log.error("telegram rejected message: %s", str(resp)[:200])
                ok = False
        except ScanError as e:
            log.error("telegram send failed: %s", e)
            ok = False
    return ok


def load_state() -> Dict[str, float]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_state(state: Dict[str, float]) -> None:
    try:
        cutoff = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cutoff}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        log.warning("could not save state: %s", e)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Crypto futures signal scanner (analysis only)")
    ap.add_argument("--symbols", default=os.getenv("SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true", help="print only, do not send to Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        log.error("no symbols given")
        return 2

    results: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    for sym in symbols:
        try:
            results.append(analyze_symbol(sym, args.min_score))
            log.info("%s scanned", sym)
        except ScanError as e:
            log.error("%s: %s", sym, e)
            errors.append((sym, str(e)))
        except Exception as e:
            log.exception("%s: unexpected error", sym)
            errors.append((sym, "unexpected error: %s" % e))

    send = (lambda t: print(t) or True) if args.dry_run else send_telegram

    if not results:
        send("❗ <b>اسکن ناموفق بود</b>\n" + "\n".join(
            "%s: %s" % (html.escape(s), html.escape(e[:150])) for s, e in errors))
        return 1

    state = load_state()
    sent = 0
    for res in results:
        sig = res["signal"]
        if not sig:
            continue
        key = "%s:%s" % (res["symbol"], sig["side"])
        if time.time() - state.get(key, 0.0) < COOLDOWN_H * 3600:
            log.info("%s in cooldown, skipped", key)
            res["signal"] = None
            res["global_blockers"] = list(res["global_blockers"]) + ["سیگنال مشابه اخیراً ارسال شده (cooldown)"]
            continue
        if send(format_signal(res, sig)) or args.dry_run:
            state[key] = time.time()
            sent += 1
    if not args.dry_run:
        save_state(state)
    send(format_report(results, errors, sent))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)