from datetime import datetime, timezone
import json
import logging
import os
from typing import List, Optional, Tuple
import requests

from .config import ScannerConfig
from .models import CoinScanResult, DirectionSetup, ScanReport

logger = logging.getLogger(__name__)


def _format_direction_block(setup: DirectionSetup) -> List[str]:
    lines: List[str] = []
    status_label = "فعال (SIGNAL)" if setup.status == "SIGNAL" else "NO TRADE"
    lines.append(f"  [{setup.direction}] وضعیت: {status_label} | امتیاز همگرایی: {setup.confluence_score}/100")

    if setup.status == "NO TRADE":
        lines.append("    - نقطه/محدوده ورود: NO TRADE")
        lines.append("    - حد ضرر: NO TRADE")
        lines.append("    - تارگت ۱ / تارگت ۲: NO TRADE / NO TRADE")
        lines.append("    - Risk/Reward: NO TRADE")
        if setup.no_trade_reasons:
            lines.append(f"    - دلیل NO TRADE: {' | '.join(setup.no_trade_reasons)}")
    else:
        entry_str = f"{setup.entry_low} - {setup.entry_high}" if setup.entry_low is not None and setup.entry_high is not None else "N/A"
        lines.append(f"    - محدوده ورود: {entry_str} (مرجع: {setup.entry_reference})")
        lines.append(f"    - حد ضرر: {setup.stop_loss}")
        lines.append(f"    - تارگت ۱: {setup.target_1} | تارگت ۲: {setup.target_2}")
        lines.append(f"    - Risk/Reward: TP1={setup.rr_tp1}R | TP2={setup.rr_tp2}R")
        lines.append(f"    - ریسک پیشنهادی: {setup.suggested_risk_pct}% (${setup.suggested_risk_usdt}) | حجم تقریبی: {setup.suggested_position_size_base}")

    if setup.rationale:
        lines.append(f"    - دلیل ورود/تحلیل: {'؛ '.join(setup.rationale[:4])}")
    if setup.activation_conditions:
        lines.append(f"    - شرایط فعال‌شدن: {'؛ '.join(setup.activation_conditions)}")
    if setup.invalidation_conditions:
        lines.append(f"    - شرایط باطل‌شدن: {'؛ '.join(setup.invalidation_conditions)}")

    return lines


def build_scan_report(
    coins: List[CoinScanResult],
    config: ScannerConfig,
    now_ms: int,
    system_errors: Optional[List[str]] = None,
) -> ScanReport:
    dt_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    valid_coins = sum(1 for c in coins if c.data_valid)
    active_long = sum(1 for c in coins if c.long_setup.status == "SIGNAL")
    active_short = sum(1 for c in coins if c.short_setup.status == "SIGNAL")

    return ScanReport(
        generated_at_utc=dt_utc,
        generated_at_ms=now_ms,
        primary_exchange="Binance USDⓈ-M Futures",
        fallback_exchange="Bybit USDT Linear Perpetual",
        live_trading_enabled=False,
        risk_per_trade_pct=config.risk_per_trade_pct,
        account_equity_usdt=config.account_equity_usdt,
        total_coins=len(coins),
        valid_coins=valid_coins,
        active_long_signals=active_long,
        active_short_signals=active_short,
        coins=coins,
        system_errors=system_errors or [],
    )


def format_text_report(report: ScanReport) -> str:
    lines: List[str] = [
        "============================================================",
        "گزارش اسکنر فیوچرز ارز دیجیتال (Perpetual SMC & Multi-TF)",
        f"زمان تولید (UTC): {report.generated_at_utc}",
        f"منبع اصلی: {report.primary_exchange} | پشتیبان: {report.fallback_exchange}",
        f"ریسک پیشنهادی: {report.risk_per_trade_pct}% در هر معامله | معاملات واقعی: غیرفعال (فقط تحلیل)",
        f"تعداد ارزها: {report.total_coins} | داده معتبر: {report.valid_coins} | سیگنال LONG: {report.active_long_signals} | سیگنال SHORT: {report.active_short_signals}",
        "============================================================",
    ]

    if report.system_errors:
        lines.append("خطاهای سیستمی:")
        for err in report.system_errors:
            lines.append(f"  * {err}")
        lines.append("------------------------------------------------------------")

    for coin in report.coins:
        tf_str = ", ".join(f"{k}:{v}" for k, v in coin.timeframe_bias.items())
        fr_str = f"{coin.funding_rate:.6f}" if coin.funding_rate is not None else "N/A"
        sp_str = f"{coin.spread_pct:.4f}%" if coin.spread_pct is not None else "N/A"
        tradeable_str = "قابل معامله" if coin.market_tradeable else "غیرقابل معامله / NO TRADE"

        lines.append("")
        lines.append(f"ارز: {coin.symbol}USDT | منبع: {coin.exchange_source} | وضعیت بازار: {tradeable_str}")
        lines.append(f"  تایم‌فریم‌ها: [{tf_str}] | هم‌جهتی: {coin.mtf_alignment} | Funding: {fr_str} | Spread: {sp_str}")
        if coin.data_issues:
            lines.append(f"  هشدار داده: {' | '.join(coin.data_issues)}")
        lines.extend(_format_direction_block(coin.long_setup))
        lines.extend(_format_direction_block(coin.short_setup))

    lines.append("")
    lines.append("============================================================")
    return "\n".join(lines)


def save_reports(report: ScanReport, output_dir: str) -> Tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "scan_report.json")
    txt_path = os.path.join(output_dir, "scan_report.txt")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

    text_content = format_text_report(report)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text_content)

    return json_path, txt_path


def send_telegram_report(report: ScanReport, bot_token: str, chat_id: str) -> bool:
    if not bot_token or not chat_id:
        logger.info("Telegram credentials not provided; skipping Telegram dispatch.")
        return False

    summary_lines = [
        "📊 <b>گزارش اسکنر فیوچرز (Perpetual SMC)</b>",
        f"🕒 زمان: <code>{report.generated_at_utc}</code>",
        f"🛡 ریسک پیشنهادی: <b>{report.risk_per_trade_pct}%</b> | معاملات واقعی: <b>غیرفعال</b>",
        f"✅ داده معتبر: {report.valid_coins}/{report.total_coins} | 🟢 LONG: {report.active_long_signals} | 🔴 SHORT: {report.active_short_signals}",
    ]

    signals_found = []
    for c in report.coins:
        for s in (c.long_setup, c.short_setup):
            if s.status == "SIGNAL":
                signals_found.append(
                    f"• <b>{c.symbol}USDT ({s.direction})</b> | ورود: <code>{s.entry_low}-{s.entry_high}</code> | SL: <code>{s.stop_loss}</code> | TP1: <code>{s.target_1}</code> | TP2: <code>{s.target_2}</code> | RR: {s.rr_tp1}/{s.rr_tp2} | امتیاز: {s.confluence_score}"
                )

    if signals_found:
        summary_lines.append("")
        summary_lines.append("<b>سیگنال‌های فعال:</b>")
        summary_lines.extend(signals_found[:20])
    else:
        summary_lines.append("")
        summary_lines.append("⚪️ در این اسکن هیچ سیگنال تاییدشده‌ای ثبت نشد (اکثریت/همه سناریوها: <b>NO TRADE</b>).")

    message = "\n".join(summary_lines)
    if len(message) > 3900:
        message = message[:3880] + "\n... (گزارش کامل در فایل JSON/TXT ذخیره شد)"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Failed to send Telegram report: %s", exc)
        return False
