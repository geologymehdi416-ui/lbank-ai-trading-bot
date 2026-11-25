#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LBank Futures Smart Scanner
Reads ONLY collector.py outputs:
  data/prices.jsonl
  data/orderbooks.jsonl
  data/contracts.json

Important:
- These are periodic market snapshots, NOT OHLCV candles.
- No EMA/RSI/MACD/BOS/OB/FVG/Fibonacci indicators are fabricated.
- A (confirmed trade signal) is deliberately disabled until verified Futures
  candles and market-structure data are available.
- B means a conditional watch candidate, not an instruction to trade.
- This script does not place orders or calculate leverage/position size.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path("data")
PRICE_FILE = DATA_DIR / "prices.jsonl"
BOOK_FILE = DATA_DIR / "orderbooks.jsonl"
CONTRACT_FILE = DATA_DIR / "contracts.json"

OUTPUT_FILE = Path("signals_futures_lbank.json")
REPORT_FILE = Path("report.txt")
PLANS_FILE = Path("plans_state.json")
LOG_FILE = Path("signals_log.csv")

QUOTE = os.getenv("LBANK_QUOTE_CURRENCY", "USDT").upper()
MAX_SNAPSHOT_AGE_MIN = float(os.getenv("MAX_SNAPSHOT_AGE_MIN", "15"))
MAX_BOOK_AGE_MIN = float(os.getenv("MAX_BOOK_AGE_MIN", "15"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
MIN_DEPTH_QUOTE = float(os.getenv("MIN_DEPTH_QUOTE", "1000"))
MIN_SCORE_B = int(os.getenv("MIN_SCORE_B", "60"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "100"))


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


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                continue
    return rows


def age_minutes(row: dict, now: datetime) -> Optional[float]:
    dt = iso_dt(row.get("timestamp"))
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def latest_by_symbol(rows: List[dict]) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        old = result.get(symbol)
        dt = iso_dt(row.get("timestamp"))
        old_dt = iso_dt(old.get("timestamp")) if old else None
        if old is None or (dt and (old_dt is None or dt > old_dt)):
            result[symbol] = row
    return result


def group_snapshots(rows: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
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
    if not current or current <= 0:
        return None
    old = price_at_or_before(rows, now.replace() - __import__("datetime").timedelta(minutes=minutes))
    if not old or old <= 0:
        return None
    return (current / old - 1.0) * 100.0


def book_quality(book: Optional[dict]) -> dict:
    if not book:
        return {"valid": False, "reason": "order book unavailable"}
    bid = num(book.get("bestBid"))
    ask = num(book.get("bestAsk"))
    spread = num(book.get("spreadPct"))
    bid_depth = num(book.get("bidDepthQuote"), 0.0) or 0.0
    ask_depth = num(book.get("askDepthQuote"), 0.0) or 0.0
    if not bid or not ask or bid <= 0 or ask <= bid:
        return {"valid": False, "reason": "invalid bid/ask"}
    total = bid_depth + ask_depth
    imbalance = ((bid_depth - ask_depth) / total) if total > 0 else None
    return {
        "valid": True,
        "spread_pct": spread,
        "bid_depth_quote": bid_depth,
        "ask_depth_quote": ask_depth,
        "total_depth_quote": total,
        "depth_imbalance": imbalance,
        "best_bid": bid,
        "best_ask": ask,
        "mid_price": num(book.get("midPrice")),
    }


def round_to_tick(value: float, tick: Optional[float]) -> float:
    if not tick or tick <= 0:
        return value
    return round(value / tick) * tick


def analyze_symbol(
    symbol: str,
    rows: List[dict],
    snapshot: dict,
    book: Optional[dict],
    contract: dict,
    now: datetime,
) -> dict:
    rows = rows[-500:]
    px = num(snapshot.get("lastPrice"))
    snap_age = age_minutes(snapshot, now)
    book_age = age_minutes(book, now) if book else None
    quality = book_quality(book)

    reasons: List[str] = []
    data_ok = px is not None and px > 0 and snap_age is not None and snap_age <= MAX_SNAPSHOT_AGE_MIN
    if not data_ok:
        reasons.append("قیمت نامعتبر یا داده بازار قدیمی است")
    if not book or book_age is None or book_age > MAX_BOOK_AGE_MIN:
        reasons.append("دفتر سفارش موجود نیست یا قدیمی است")
    if not quality["valid"]:
        reasons.append(quality.get("reason", "دفتر سفارش نامعتبر"))
    else:
        if quality["spread_pct"] is None or quality["spread_pct"] > MAX_SPREAD_PCT:
            reasons.append("اسپرد بیش از حد مجاز")
        if quality["total_depth_quote"] < MIN_DEPTH_QUOTE:
            reasons.append("عمق دفتر سفارش کمتر از حداقل تعیین‌شده")

    r15 = sampled_return(rows, 15, now)
    r60 = sampled_return(rows, 60, now)
    r240 = sampled_return(rows, 240, now)
    daily = num(snapshot.get("openPrice24h"))
    r24 = ((px / daily) - 1.0) * 100.0 if px and daily and daily > 0 else None

    # Directional context is based on sampled price changes, not candles.
    directions = []
    for value in (r240, r60, r15):
        if value is None:
            directions.append("UNKNOWN")
        elif value > 0.03:
            directions.append("UP")
        elif value < -0.03:
            directions.append("DOWN")
        else:
            directions.append("FLAT")

    # Score is a data-quality / directional-confluence score, not win probability.
    candidates = []
    for side in ("LONG", "SHORT"):
        sign = 1 if side == "LONG" else -1
        score = 0
        components = {}

        def award(name: str, condition: bool, points: int) -> None:
            nonlocal score
            components[name] = {"passed": bool(condition), "points": points if condition else 0}
            if condition:
                score += points

        award("4h_sampled_direction", r240 is not None and sign * r240 > 0.05, 25)
        award("1h_sampled_direction", r60 is not None and sign * r60 > 0.03, 20)
        award("15m_sampled_direction", r15 is not None and sign * r15 > 0.01, 10)
        award("24h_context", r24 is not None and sign * r24 > 0, 10)

        imb = quality.get("depth_imbalance")
        award("orderbook_imbalance", imb is not None and sign * imb >= 0.08, 20)
        award("spread", quality.get("spread_pct") is not None and quality["spread_pct"] <= MAX_SPREAD_PCT, 10)
        award("depth", quality.get("total_depth_quote", 0) >= MIN_DEPTH_QUOTE, 5)

        # A B-candidate requires sufficient sampled history and fresh market/book data.
        history_ok = r15 is not None and r60 is not None and r240 is not None
        hard_ok = data_ok and history_ok and book_age is not None and book_age <= MAX_BOOK_AGE_MIN
        hard_ok = hard_ok and quality["valid"]
        hard_ok = hard_ok and quality.get("spread_pct") is not None
        hard_ok = hard_ok and quality["spread_pct"] <= MAX_SPREAD_PCT
        hard_ok = hard_ok and quality.get("total_depth_quote", 0) >= MIN_DEPTH_QUOTE

        # Conditional breakout levels from sampled last-price observations only.
        # These are explicitly NOT candle highs/lows or structural support/resistance.
        cutoff = now - __import__("datetime").timedelta(minutes=60)
        recent = [
            num(x.get("lastPrice")) for x in rows
            if iso_dt(x.get("timestamp")) and iso_dt(x.get("timestamp")) >= cutoff
        ]
        recent = [x for x in recent if x and x > 0]
        entry = sl = tp1 = tp2 = None
        trigger = None
        rr1 = rr2 = None
        if len(recent) >= 6 and px:
            observed_low, observed_high = min(recent), max(recent)
            tick = num(contract.get("priceTick"))
            # A trigger just outside the observed sampled range, never an immediate entry.
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
            if risk > 0 and tp1 and tp2:
                rr1 = abs(tp1 - entry) / risk
                rr2 = abs(tp2 - entry) / risk
            else:
                entry = sl = tp1 = tp2 = trigger = None

        if not history_ok:
            candidate_reasons = ["تاریخچه نمونه‌های قیمت برای بازه‌های ۱۵ دقیقه، ۱ ساعت و ۴ ساعت کافی نیست"]
        else:
            candidate_reasons = []
        if not hard_ok:
            candidate_reasons.extend(reasons)
        if entry is None:
            candidate_reasons.append("برای طرح شرطی، نمونه‌های قیمت یک‌ساعته کافی نیست")
        if score < MIN_SCORE_B:
            candidate_reasons.append(f"امتیاز همگرایی کمتر از حد {MIN_SCORE_B} است")

        status = "B_CONDITIONAL" if hard_ok and score >= MIN_SCORE_B and entry is not None else "C_NO_TRADE"
        candidates.append({
            "side": side,
            "score": score,
            "status": status,
            "components": components,
            "sampled_returns_pct": {
                "15m": r15, "1h": r60, "4h": r240, "24h_from_open": r24
            },
            "conditional_plan": {
                "entry_trigger": trigger,
                "entry_reference": entry,
                "stop_loss": sl,
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "rr_tp1": rr1,
                "rr_tp2": rr2,
                "trigger_rule": (
                    "فقط پس از عبور قیمت از سطح محرک و تأیید با داده معتبر؛ "
                    "این سطح از نمونه‌های قیمت استخراج شده، نه کندل"
                ) if entry is not None else None,
                "level_type": "sampled_last_price_range_not_candle_structure",
            },
            "reasons": candidate_reasons,
        })

    best = max(candidates, key=lambda x: (x["score"], x["status"] == "B_CONDITIONAL"))
    return {
        "symbol": symbol,
        "status": best["status"],
        "side": best["side"],
        "score": best["score"],
        "price": px,
        "snapshot_age_minutes": snap_age,
        "orderbook_age_minutes": book_age,
        "market_context": {
            "returns_pct": {"15m": r15, "1h": r60, "4h": r240, "24h": r24},
            "sampled_direction_4h_1h_15m": directions,
            "orderbook": quality,
        },
        "analysis": best,
        "data_quality_reasons": reasons,
        "contract": {
            "priceTick": contract.get("priceTick"),
            "volumeTick": contract.get("volumeTick"),
            "volumeMultiple": contract.get("volumeMultiple"),
            "minOrderVolume": contract.get("minOrderVolume"),
            "minOrderCost": contract.get("minOrderCost"),
            "defaultLeverage": contract.get("defaultLeverage"),
        },
        "updated_at": now.isoformat(),
    }


def write_csv(results: List[dict]) -> None:
    fields = [
        "timestamp", "symbol", "status", "side", "score", "price",
        "entry_trigger", "stop_loss", "take_profit_1", "take_profit_2",
        "rr_tp1", "rr_tp2", "reason",
    ]
    exists = LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for row in results:
            plan = row.get("analysis", {}).get("conditional_plan", {})
            reasons = row.get("analysis", {}).get("reasons", [])
            writer.writerow({
                "timestamp": row.get("updated_at"),
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "side": row.get("side"),
                "score": row.get("score"),
                "price": row.get("price"),
                "entry_trigger": plan.get("entry_trigger"),
                "stop_loss": plan.get("stop_loss"),
                "take_profit_1": plan.get("take_profit_1"),
                "take_profit_2": plan.get("take_profit_2"),
                "rr_tp1": plan.get("rr_tp1"),
                "rr_tp2": plan.get("rr_tp2"),
                "reason": " | ".join(reasons),
            })


def make_report(results: List[dict], generated_at: str, input_errors: List[str]) -> str:
    a_count = 0  # Intentionally disabled until verified Futures candle feed exists.
    b_rows = [x for x in results if x.get("status") == "B_CONDITIONAL"]
    c_rows = [x for x in results if x.get("status") == "C_NO_TRADE"]
    lines = [
        "LBANK FUTURES SMART SCANNER",
        f"Generated: {generated_at}",
        "Data source: LBank Contract Futures public API only",
        "",
        f"Markets analyzed: {len(results)}",
        f"A - confirmed signals: {a_count} (DISABLED: no verified Futures candles)",
        f"B - conditional watch candidates: {len(b_rows)}",
        f"C - no-trade / rejected: {len(c_rows)}",
        "",
        "IMPORTANT:",
        "- No OHLCV candles were received or fabricated.",
        "- EMA, RSI, MACD, BOS, OB, FVG and Fibonacci are not calculated.",
        "- B candidates are conditional watch ideas based on sampled prices and order-book snapshots.",
        "- Do not place an order from this report alone.",
        "- Leverage and position sizing are not enabled in this version.",
        "",
    ]
    if input_errors:
        lines += ["INPUT WARNINGS:"] + [f"- {x}" for x in input_errors] + [""]
    lines.append("TOP CONDITIONAL CANDIDATES:")
    for row in sorted(b_rows, key=lambda x: x.get("score", 0), reverse=True)[:5]:
        plan = row.get("analysis", {}).get("conditional_plan", {})
        lines.append(
            f"- {row['symbol']} {row['side']} score={row['score']} "
            f"price={row.get('price')} trigger={plan.get('entry_trigger')} "
            f"SL={plan.get('stop_loss')} TP1={plan.get('take_profit_1')} "
            f"TP2={plan.get('take_profit_2')}"
        )
    if not b_rows:
        lines.append("- No conditional candidate passed the required data/liquidity filters.")
    lines += ["", "TOP REJECTION REASONS:"]
    counts: Dict[str, int] = defaultdict(int)
    for row in c_rows:
        for reason in row.get("analysis", {}).get("reasons", []):
            counts[reason] += 1
        for reason in row.get("data_quality_reasons", []):
            counts[reason] += 1
    for reason, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:8]:
        lines.append(f"- {reason}: {count}")
    if not counts:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> None:
    now = now_utc()
    generated = now.isoformat()
    errors: List[str] = []

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
        str(c.get("symbol", "")).upper(): c
        for c in contracts_raw if isinstance(c, dict) and c.get("symbol")
    }

    symbols = sorted(set(snapshots) & set(contracts))
    results: List[dict] = []
    for symbol in symbols[:MAX_SYMBOLS]:
        contract = contracts[symbol]
        settlement = str(contract.get("clearCurrency", "")).upper()
        if settlement and settlement != QUOTE:
            continue
        results.append(analyze_symbol(
            symbol=symbol,
            rows=histories.get(symbol, []),
            snapshot=snapshots[symbol],
            book=books.get(symbol),
            contract=contract,
            now=now,
        ))

    # Explicit A/B/C accounting. A is always zero until candle data is verified.
    report_obj = {
        "scanner": "LBank Futures Smart Scanner",
        "generated_at": generated,
        "source": "LBank Contract Futures public REST API",
        "mode": "ANALYSIS_ONLY_NO_ORDERS",
        "candles_available": False,
        "confirmed_signals_enabled": False,
        "leverage_enabled": False,
        "summary": {
            "markets_analyzed": len(results),
            "A_confirmed": 0,
            "B_conditional": sum(x["status"] == "B_CONDITIONAL" for x in results),
            "C_no_trade": sum(x["status"] == "C_NO_TRADE" for x in results),
            "input_warnings": len(errors),
        },
        "input_warnings": errors,
        "results": results,
        "disclaimer": (
            "Snapshot-based research only. No order execution. "
            "Conditional levels are not candle-based support/resistance. "
            "Do not use these results as a standalone trading signal."
        ),
    }

    OUTPUT_FILE.write_text(
        json.dumps(report_obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    PLANS_FILE.write_text(
        json.dumps({
            "generated_at": generated,
            "source": "LBank Futures snapshots",
            "plans": [
                {
                    "symbol": x["symbol"],
                    "status": x["status"],
                    "side": x["side"],
                    "score": x["score"],
                    "plan": x["analysis"].get("conditional_plan", {}),
                }
                for x in results if x["status"] == "B_CONDITIONAL"
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(results)
    report = make_report(results, generated, errors)
    REPORT_FILE.write_text(report, encoding="utf-8")

    print(json.dumps(report_obj["summary"], ensure_ascii=False, indent=2))
    print(f"Saved: {OUTPUT_FILE.resolve()}")
    print(f"Saved: {REPORT_FILE.resolve()}")
    print(f"Saved: {PLANS_FILE.resolve()}")
    print(f"Saved: {LOG_FILE.resolve()}")


if __name__ == "__main__":
    main()
