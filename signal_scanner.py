#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LBank Futures Smart Scanner - 20 asset watchlist edition.

Reads only LBank Contract Futures snapshots/order books.
Snapshots are NOT OHLCV candles. No EMA/RSI/MACD/BOS/OB/FVG/Fibonacci
or confirmed A signals are fabricated. B is a conditional research watch,
not an instruction to trade.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path("data")
PRICE_FILE = DATA_DIR / "prices.jsonl"
BOOK_FILE = DATA_DIR / "orderbooks.jsonl"
CONTRACT_FILE = DATA_DIR / "contracts.json"
OUTPUT_FILE = Path("signals_futures_lbank.json")
REPORT_FILE = Path("report.txt")
PLANS_FILE = Path("plans_state.json")
LOG_FILE = Path("signals_log.csv")

QUOTE = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()
WATCHLIST = [
    x.strip().upper()
    for x in os.getenv(
        "LBANK_WATCHLIST",
        "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,AVAX,LINK,LTC,HYPE,SUI,AAVE,UNI,NEAR,ICP,RENDER,ONDO,PYTH,JUP"
    ).split(",") if x.strip()
][:20]
MAX_SNAPSHOT_AGE_MIN = float(os.getenv("MAX_SNAPSHOT_AGE_MIN", "15"))
MAX_BOOK_AGE_MIN = float(os.getenv("MAX_BOOK_AGE_MIN", "15"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
MIN_DEPTH_QUOTE = float(os.getenv("MIN_DEPTH_QUOTE", "1000"))
MIN_SCORE_B = int(os.getenv("MIN_SCORE_B", "60"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def norm_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def base_asset(row: dict) -> str:
    base = str(row.get("baseCurrency") or "").upper().strip()
    if base:
        return base
    symbol = norm_symbol(row.get("symbol"))
    quote = norm_symbol(QUOTE)
    return symbol[:-len(quote)] if quote and symbol.endswith(quote) else symbol


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl(path: Path) -> List[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line.strip())
                if isinstance(item, dict):
                    rows.append(item)
            except (json.JSONDecodeError, AttributeError):
                continue
    return rows


def age_minutes(row: Optional[dict], now: datetime) -> Optional[float]:
    if not row:
        return None
    dt = iso_dt(row.get("timestamp"))
    return max(0.0, (now - dt).total_seconds() / 60) if dt else None


def latest_by_symbol(rows: List[dict]) -> Dict[str, dict]:
    result = {}
    for row in rows:
        symbol = norm_symbol(row.get("symbol"))
        dt = iso_dt(row.get("timestamp"))
        if not symbol or not dt:
            continue
        old_dt = iso_dt(result[symbol].get("timestamp")) if symbol in result else None
        if symbol not in result or old_dt is None or dt > old_dt:
            result[symbol] = row
    return result


def group_snapshots(rows: List[dict]) -> Dict[str, List[dict]]:
    grouped = defaultdict(list)
    for row in rows:
        symbol = norm_symbol(row.get("symbol"))
        dt = iso_dt(row.get("timestamp"))
        price = num(row.get("lastPrice"))
        if symbol and dt and price and price > 0:
            grouped[symbol].append(row)
    for symbol in grouped:
        grouped[symbol].sort(key=lambda r: iso_dt(r["timestamp"]))
    return grouped


def price_at_or_before(rows: List[dict], target: datetime) -> Optional[float]:
    chosen = None
    for row in rows:
        dt = iso_dt(row.get("timestamp"))
        if dt and dt <= target:
            p = num(row.get("lastPrice"))
            if p and p > 0:
                chosen = p
        elif dt and dt > target:
            break
    return chosen


def sampled_return(rows: List[dict], minutes: int, now: datetime) -> Optional[float]:
    if not rows:
        return None
    current = num(rows[-1].get("lastPrice"))
    old = price_at_or_before(rows, now - timedelta(minutes=minutes))
    if not current or current <= 0 or not old or old <= 0:
        return None
    return (current / old - 1) * 100


def unique_reasons(items: List[str]) -> List[str]:
    return list(dict.fromkeys(x for x in items if x))


def book_quality(book: Optional[dict]) -> dict:
    if not book:
        return {"valid": False, "missing": True, "reason": "دفتر سفارش دریافت نشده"}
    bid, ask = num(book.get("bestBid")), num(book.get("bestAsk"))
    spread = num(book.get("spreadPct"))
    bid_depth = num(book.get("bidDepthQuote"), 0.0) or 0.0
    ask_depth = num(book.get("askDepthQuote"), 0.0) or 0.0
    if not bid or not ask or bid <= 0 or ask <= bid:
        return {"valid": False, "missing": False, "reason": "ساختار دفتر سفارش یا قیمت Bid/Ask نامعتبر است"}
    total = bid_depth + ask_depth
    return {
        "valid": True, "missing": False, "spread_pct": spread,
        "bid_depth_quote": bid_depth, "ask_depth_quote": ask_depth,
        "total_depth_quote": total,
        "depth_imbalance": (bid_depth - ask_depth) / total if total > 0 else None,
        "best_bid": bid, "best_ask": ask, "mid_price": num(book.get("midPrice")),
    }


def round_to_tick(value: float, tick: Optional[float]) -> float:
    if not tick or tick <= 0:
        return value
    return round(value / tick) * tick


def analyze_symbol(symbol: str, rows: List[dict], snapshot: dict,
                   book: Optional[dict], contract: dict, now: datetime) -> dict:
    rows = rows[-1000:]
    px = num(snapshot.get("lastPrice"))
    snap_age = age_minutes(snapshot, now)
    book_age = age_minutes(book, now)
    quality = book_quality(book)

    data_reasons = []
    if px is None or px <= 0 or snap_age is None or snap_age > MAX_SNAPSHOT_AGE_MIN:
        data_reasons.append("قیمت نامعتبر یا snapshot قدیمی است")
    if book is None or book_age is None or book_age > MAX_BOOK_AGE_MIN:
        data_reasons.append("دفتر سفارش موجود نیست یا قدیمی است")

    r15 = sampled_return(rows, 15, now)
    r60 = sampled_return(rows, 60, now)
    r240 = sampled_return(rows, 240, now)
    daily_open = num(snapshot.get("openPrice24h"))
    r24 = (px / daily_open - 1) * 100 if px and daily_open and daily_open > 0 else None

    history_reasons = []
    if r15 is None:
        history_reasons.append("تاریخچه قیمت ۱۵ دقیقه‌ای هنوز جمع نشده")
    if r60 is None:
        history_reasons.append("تاریخچه قیمت یک‌ساعته هنوز جمع نشده")
    if r240 is None:
        history_reasons.append("تاریخچه قیمت چهارساعته هنوز جمع نشده")

    candidates = []
    for side in ("LONG", "SHORT"):
        sign = 1 if side == "LONG" else -1
        score = 0
        components = {}

        def award(name: str, passed: bool, points: int):
            nonlocal score
            components[name] = {"passed": bool(passed), "points": points if passed else 0}
            if passed:
                score += points

        award("4h_sampled_direction", r240 is not None and sign * r240 > 0.05, 25)
        award("1h_sampled_direction", r60 is not None and sign * r60 > 0.03, 20)
        award("15m_sampled_direction", r15 is not None and sign * r15 > 0.01, 10)
        award("24h_context", r24 is not None and sign * r24 > 0, 10)

        imbalance = quality.get("depth_imbalance")
        award("orderbook_imbalance", imbalance is not None and sign * imbalance >= 0.08, 20)
        award("spread", quality.get("spread_pct") is not None and quality["spread_pct"] <= MAX_SPREAD_PCT, 10)
        award("depth", quality.get("total_depth_quote", 0) >= MIN_DEPTH_QUOTE, 5)

        # The conditional setup requires complete 4h/1h/15m sampled history.
        history_ok = r15 is not None and r60 is not None and r240 is not None
        snapshot_ok = px is not None and px > 0 and snap_age is not None and snap_age <= MAX_SNAPSHOT_AGE_MIN
        book_fresh = book is not None and book_age is not None and book_age <= MAX_BOOK_AGE_MIN
        book_structurally_valid = quality.get("valid", False)
        liquidity_ok = (
            book_structurally_valid
            and quality.get("spread_pct") is not None
            and quality["spread_pct"] <= MAX_SPREAD_PCT
            and quality.get("total_depth_quote", 0) >= MIN_DEPTH_QUOTE
        )

        recent_cutoff = now - timedelta(minutes=60)
        recent = [
            num(x.get("lastPrice")) for x in rows
            if iso_dt(x.get("timestamp")) and iso_dt(x.get("timestamp")) >= recent_cutoff
        ]
        recent = [x for x in recent if x is not None and x > 0]

        entry = sl = tp1 = tp2 = trigger = rr1 = rr2 = None
        if len(recent) >= 6 and px:
            observed_low, observed_high = min(recent), max(recent)
            tick = num(contract.get("priceTick"))
            if side == "LONG":
                trigger = round_to_tick(observed_high * 1.0005, tick)
                sl = round_to_tick(observed_low * 0.999, tick)
                entry = trigger
                risk = entry - sl
                if risk > 0:
                    tp1, tp2 = entry + 1.5 * risk, entry + 2.0 * risk
            else:
                trigger = round_to_tick(observed_low * 0.9995, tick)
                sl = round_to_tick(observed_high * 1.001, tick)
                entry = trigger
                risk = sl - entry
                if risk > 0:
                    tp1, tp2 = entry - 1.5 * risk, entry - 2.0 * risk
            if risk > 0 and tp1 is not None and tp2 is not None:
                rr1, rr2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
            else:
                entry = sl = tp1 = tp2 = trigger = None

        missing_inputs = []
        if not snapshot_ok:
            missing_inputs.extend(data_reasons[:1])
        if not history_ok:
            missing_inputs.extend(history_reasons)
        if not book_fresh or not book_structurally_valid:
            missing_inputs.append(quality.get("reason", "دفتر سفارش نامعتبر") if not book_structurally_valid else "دفتر سفارش قدیمی است")
        if entry is None:
            missing_inputs.append("نمونه‌های کافی برای ساخت سطح محرک یک‌ساعته وجود ندارد")
        missing_inputs = unique_reasons(missing_inputs)

        if missing_inputs:
            status = "INSUFFICIENT_DATA"
            reasons = missing_inputs
        elif not liquidity_ok:
            status = "C_NO_TRADE"
            reasons = []
            if quality.get("spread_pct") is None or quality["spread_pct"] > MAX_SPREAD_PCT:
                reasons.append("اسپرد بیش از حد مجاز")
            if quality.get("total_depth_quote", 0) < MIN_DEPTH_QUOTE:
                reasons.append("عمق دفتر سفارش کمتر از حداقل تعیین‌شده")
        elif score < MIN_SCORE_B:
            status = "C_NO_TRADE"
            reasons = [f"امتیاز همگرایی کمتر از حد {MIN_SCORE_B} است"]
        else:
            status = "B_CONDITIONAL"
            reasons = []

        candidates.append({
            "side": side, "score": score, "status": status,
            "components": components,
            "sampled_returns_pct": {"15m": r15, "1h": r60, "4h": r240, "24h_from_open": r24},
            "conditional_plan": {
                "entry_trigger": trigger, "entry_reference": entry,
                "stop_loss": sl, "take_profit_1": tp1, "take_profit_2": tp2,
                "rr_tp1": rr1, "rr_tp2": rr2,
                "trigger_rule": (
                    "ورود فقط پس از شکست سطح محرک و تأیید مجدد؛ این سطوح از نمونه‌های قیمت‌اند، نه کندل"
                ) if entry is not None else None,
                "level_type": "sampled_last_price_range_not_candle_structure",
            },
            "reasons": reasons,
        })

    # Prefer a real B candidate; otherwise show the best-scoring side and its reason.
    best = max(candidates, key=lambda x: (x["status"] == "B_CONDITIONAL", x["score"]))
    return {
        "symbol": symbol,
        "base_asset": base_asset(snapshot),
        "status": best["status"], "side": best["side"], "score": best["score"],
        "price": px, "snapshot_age_minutes": snap_age, "orderbook_age_minutes": book_age,
        "market_context": {
            "returns_pct": {"15m": r15, "1h": r60, "4h": r240, "24h": r24},
            "orderbook": quality,
        },
        "analysis": best,
        "alternate_side": next(x for x in candidates if x["side"] != best["side"]),
        "updated_at": now.isoformat(),
        "contract": {
            "priceTick": contract.get("priceTick"), "volumeTick": contract.get("volumeTick"),
            "volumeMultiple": contract.get("volumeMultiple"), "minOrderVolume": contract.get("minOrderVolume"),
            "minOrderCost": contract.get("minOrderCost"), "defaultLeverage": contract.get("defaultLeverage"),
        },
    }


def write_csv(results: List[dict]) -> None:
    fields = ["timestamp", "symbol", "status", "side", "score", "price",
              "entry_trigger", "stop_loss", "take_profit_1", "take_profit_2",
              "rr_tp1", "rr_tp2", "reason"]
    exists = LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for row in results:
            plan = row.get("analysis", {}).get("conditional_plan", {})
            reasons = row.get("analysis", {}).get("reasons", [])
            writer.writerow({
                "timestamp": row.get("updated_at"), "symbol": row.get("symbol"),
                "status": row.get("status"), "side": row.get("side"),
                "score": row.get("score"), "price": row.get("price"),
                "entry_trigger": plan.get("entry_trigger"), "stop_loss": plan.get("stop_loss"),
                "take_profit_1": plan.get("take_profit_1"), "take_profit_2": plan.get("take_profit_2"),
                "rr_tp1": plan.get("rr_tp1"), "rr_tp2": plan.get("rr_tp2"),
                "reason": " | ".join(reasons),
            })


def make_report(results: List[dict], generated_at: str, input_errors: List[str]) -> str:
    b_rows = [x for x in results if x["status"] == "B_CONDITIONAL"]
    insufficient = [x for x in results if x["status"] == "INSUFFICIENT_DATA"]
    rejected = [x for x in results if x["status"] == "C_NO_TRADE"]
    lines = [
        "LBANK FUTURES SMART SCANNER - 20 ASSET WATCHLIST",
        f"Generated: {generated_at}",
        "Data source: LBank Contract Futures public API only",
        "",
        f"Watchlist assets: {len(WATCHLIST)} ({', '.join(WATCHLIST)})",
        f"Markets analyzed: {len(results)}",
        "A - confirmed signals: 0 (DISABLED: no verified Futures candles)",
        f"B - conditional watch candidates: {len(b_rows)}",
        f"INSUFFICIENT_DATA - waiting for data: {len(insufficient)}",
        f"C - rejected by filters: {len(rejected)}",
        "",
        "IMPORTANT:",
        "- No OHLCV candles were received or fabricated.",
        "- EMA, RSI, MACD, BOS, OB, FVG and Fibonacci are not calculated.",
        "- B candidates are conditional research setups from sampled prices/order-book snapshots, not trade orders.",
        "- Do not place an order from this report alone.",
        "- Leverage and position sizing are disabled.",
        "",
    ]
    if input_errors:
        lines += ["INPUT WARNINGS:"] + [f"- {x}" for x in input_errors] + [""]
    lines.append("TOP CONDITIONAL CANDIDATES:")
    for row in sorted(b_rows, key=lambda x: x["score"], reverse=True)[:5]:
        p = row["analysis"]["conditional_plan"]
        lines.append(
            f"- {row['symbol']} {row['side']} score={row['score']} price={row.get('price')} "
            f"trigger={p.get('entry_trigger')} SL={p.get('stop_loss')} "
            f"TP1={p.get('take_profit_1')} TP2={p.get('take_profit_2')} "
            f"RR={p.get('rr_tp1')}/{p.get('rr_tp2')}"
        )
    if not b_rows:
        lines.append("- No B candidate currently passed all data and liquidity filters.")
    lines += ["", "INSUFFICIENT DATA (first 10):"]
    for row in insufficient[:10]:
        lines.append(f"- {row['symbol']}: {'; '.join(row['analysis'].get('reasons', []))}")
    if not insufficient:
        lines.append("- None")
    counts = Counter()
    for row in rejected:
        counts.update(set(row["analysis"].get("reasons", [])))
    lines += ["", "TOP REJECTION REASONS:"]
    for reason, count in counts.most_common(8):
        lines.append(f"- {reason}: {count}")
    if not counts:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> None:
    now = now_utc()
    generated = now.isoformat()
    errors = []
    price_rows = read_jsonl(PRICE_FILE)
    book_rows = read_jsonl(BOOK_FILE)
    contract_payload = read_json(CONTRACT_FILE) or {}
    contracts_raw = contract_payload.get("contracts", []) if isinstance(contract_payload, dict) else []

    if not price_rows:
        errors.append(f"{PRICE_FILE} missing or empty")
    if not contracts_raw:
        errors.append(f"{CONTRACT_FILE} missing or has no contracts")
    if not book_rows:
        errors.append(f"{BOOK_FILE} missing or empty")

    snapshots = latest_by_symbol(price_rows)
    books = latest_by_symbol(book_rows)
    histories = group_snapshots(price_rows)
    contracts = {
        norm_symbol(c.get("symbol")): c
        for c in contracts_raw if isinstance(c, dict) and c.get("symbol")
    }

    results = []
    missing_assets = []
    for asset in WATCHLIST:
        matches = [
            (sym, c) for sym, c in contracts.items()
            if base_asset(c) == asset and str(c.get("clearCurrency") or QUOTE).upper() == QUOTE
        ]
        if not matches:
            missing_assets.append(asset)
            continue
        for symbol, contract in matches:
            snapshot = snapshots.get(symbol)
            if not snapshot:
                continue
            results.append(analyze_symbol(
                symbol=str(snapshot.get("symbol") or symbol),
                rows=histories.get(symbol, []),
                snapshot=snapshot, book=books.get(symbol),
                contract=contract, now=now,
            ))

    b_count = sum(x["status"] == "B_CONDITIONAL" for x in results)
    insufficient_count = sum(x["status"] == "INSUFFICIENT_DATA" for x in results)
    c_count = sum(x["status"] == "C_NO_TRADE" for x in results)

    report_obj = {
        "scanner": "LBank Futures Smart Scanner - 20 asset watchlist",
        "generated_at": generated, "source": "LBank Contract Futures public REST API",
        "mode": "ANALYSIS_ONLY_NO_ORDERS", "candles_available": False,
        "confirmed_signals_enabled": False, "leverage_enabled": False,
        "watchlist": WATCHLIST, "missing_watchlist_assets": missing_assets,
        "summary": {
            "markets_analyzed": len(results), "A_confirmed": 0,
            "B_conditional": b_count, "insufficient_data": insufficient_count,
            "C_no_trade": c_count, "input_warnings": len(errors),
        },
        "input_warnings": errors, "results": results,
        "disclaimer": (
            "Snapshot-based research only. No order execution. Conditional levels are not "
            "candle-based support/resistance. Do not use as a standalone trading signal."
        ),
    }

    OUTPUT_FILE.write_text(json.dumps(report_obj, ensure_ascii=False, indent=2), encoding="utf-8")
    PLANS_FILE.write_text(json.dumps({
        "generated_at": generated, "source": "LBank Futures snapshots",
        "plans": [
            {"symbol": x["symbol"], "status": x["status"], "side": x["side"],
             "score": x["score"], "plan": x["analysis"].get("conditional_plan", {})}
            for x in results if x["status"] == "B_CONDITIONAL"
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(results)
    report = make_report(results, generated, errors)
    REPORT_FILE.write_text(report, encoding="utf-8")

    print(json.dumps(report_obj["summary"], ensure_ascii=False, indent=2))
    print(f"Missing watchlist assets: {','.join(missing_assets) if missing_assets else 'none'}")
    print(f"Saved: {OUTPUT_FILE.resolve()}")
    print(f"Saved: {REPORT_FILE.resolve()}")
    print(f"Saved: {PLANS_FILE.resolve()}")
    print(f"Saved: {LOG_FILE.resolve()}")


if __name__ == "__main__":
    main()
