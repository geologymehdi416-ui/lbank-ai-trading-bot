from datetime import datetime, timezone
import html
import json
import logging
import os
from typing import List, Optional, Tuple

import requests

from .config import ScannerConfig
from .models import CoinScanResult, DirectionSetup, ScanReport

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3900


def _safe(value) -> str:
    """Escape untrusted values before inserting them into Telegram HTML."""
    return html.escape(str(value), quote=False)


def _format_number(value, precision: int = 8) -> str:
    if value is None:
        return "N/A"
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return "N/A"
        return f"{number:.{precision}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return "N/A"


def _status_label(status: str) -> str:
    labels = {
        "CONFIRMED": "تأییدشده",
        "CONDITIONAL": "مشروط",
        "NO_TRADE": "ورود نکن (NO TRADE)",
        "INSUFFICIENT_DATA": "داده ناکافی",
        # Backward compatibility with older engine output.
        "SIGNAL": "سیگنال",
    }
    return labels.get(str(status), str(status))


def _format_direction_block(setup: DirectionSetup) -> List[str]:
    lines: List[str] = []
    status = str(setup.status)
    lines.append(
        f"  [{setup.direction}] وضعیت: {_status_label(status)}"
        f" | امتیاز همگرایی: {setup.confluence_score}/100"
    )

    if status in ("NO_TRADE", "INSUFFICIENT_DATA"):
        lines.append("    - ورود / حد ضرر / تارگت: ارائه نمی‌شود")
        if setup.no_trade_reasons:
            lines.append(
                f"    - دلیل: {' | '.join(str(x) for x in setup.no_trade_reasons)}"
            )
    else:
        entry_str = (
            f"{_format_number(setup.entry_low)} - {_format_number(setup.entry_high)}"
            if setup.entry_low is not None and setup.entry_high is not None
            else "N/A"
        )
        lines.append(
            f"    - محدوده ورود: {entry_str} (مرجع: {setup.entry_reference or 'N/A'})"
        )
        lines.append(f"    - حد ضرر: {_format_number(setup.stop_loss)}")
        lines.append(
            f"    - تارگت ۱: {_format_number(setup.target_1)}"
            f" | تارگت ۲: {_format_number(setup.target_2)}"
        )
        lines.append(
            f"    - Risk/Reward: TP1={_format_number(setup.rr_tp1, 2)}R"
            f" | TP2={_format_number(setup.rr_tp2, 2)}R"
        )

        # Position sizing is informational only. Do not imply execution is enabled.
        risk_pct = getattr(setup, "suggested_risk_pct", None)
        risk_usdt = getattr(setup, "suggested_risk_usdt", None)
        position_size = getattr(setup, "suggested_position_size_base", None)
        if risk_pct is not None or risk_usdt is not None or position_size is not None:
            lines.append(
                "    - اندازه موقعیت: محاسبه‌نشده/غیرفعال تا اعتبارسنجی مدیریت ریسک"
            )

    if setup.rationale:
        lines.append(f"    - تحلیل: {'؛ '.join(str(x) for x in setup.rationale[:4])}")
    if setup.activation_conditions:
        lines.append(
            f"    - شرایط فعال‌شدن: {'؛ '.join(str(x) for x in setup.activation_conditions)}"
        )
    if setup.invalidation_conditions:
        lines.append(
            f"    - شرایط باطل‌شدن: {'؛ '.join(str(x) for x in setup.invalidation_conditions)}"
        )

    return lines


def build_scan_report(
    coins: List[CoinScanResult],
    config: ScannerConfig,
    now_ms: int,
    system_errors: Optional[List[str]] = None,
) -> ScanReport:
    dt_utc = datetime.fromtimestamp(
        now_ms / 1000.0, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    valid_coins = sum(
        1 for coin in coins
        if getattr(coin, "data_sufficient", getattr(coin, "data_valid", False))
    )

    # The legacy ScanReport schema has only active_* counters.
    # Count CONFIRMED (and legacy SIGNAL) as active; CONDITIONAL is reported separately.
    active_long = sum(
        1 for coin in coins
        if coin.long_setup.status in ("CONFIRMED", "SIGNAL")
    )
    active_short = sum(
        1 for coin in coins
        if coin.short_setup.status in ("CONFIRMED", "SIGNAL")
    )

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
    conditional_long = sum(
        1 for coin in report.coins if coin.long_setup.status == "CONDITIONAL"
    )
    conditional_short = sum(
        1 for coin in report.coins if coin.short_setup.status == "CONDITIONAL"
    )
    insufficient = sum(
        1 for coin in report.coins
        if coin.long_setup.status == "INSUFFICIENT_DATA"
        or coin.short_setup.status == "INSUFFICIENT_DATA"
    )

    lines: List[str] = [
        "============================================================",
        "گزارش اسکنر فیوچرز ارز دیجیتال (Perpetual SMC & Multi-TF)",
        f"زمان تولید (UTC): {report.generated_at_utc}",
        f"منبع اصلی: {report.primary_exchange} | پشتیبان: {report.fallback_exchange}",
        f"ریسک هدف: {report.risk_per_trade_pct}% | معاملات واقعی: غیرفعال (فقط تحلیل)",
        (
            f"تعداد ارزها: {report.total_coins} | داده کافی: {report.valid_coins}"
            f" | LONG تأییدشده: {report.active_long_signals}"
            f" | SHORT تأییدشده: {report.active_short_signals}"
        ),
        (
            f"LONG مشروط: {conditional_long} | SHORT مشروط: {conditional_short}"
            f" | موارد دارای داده ناکافی: {insufficient}"
        ),
        "توجه: امتیاز همگرایی احتمال برد نیست؛ این گزارش سفارش معاملاتی ارسال نمی‌کند.",
        "============================================================",
    ]

    if report.system_errors:
        lines.append("خطاهای سیستمی:")
        for err in report.system_errors:
            lines.append(f"  * {err}")
        lines.append("------------------------------------------------------------")

    for coin in report.coins:
        tf_str = ", ".join(
            f"{key}:{value}" for key, value in coin.timeframe_bias.items()
        )
        funding = (
            f"{coin.funding_rate:.6f}"
            if coin.funding_rate is not None else "دریافت نشد"
        )
        spread = (
            f"{coin.spread_pct:.4f}%"
            if coin.spread_pct is not None else "N/A"
        )
        tradeable = (
            "قابل بررسی"
            if coin.market_tradeable else "غیرقابل معامله / NO TRADE"
        )

        lines.append("")
        lines.append(
            f"ارز: {coin.symbol}USDT | منبع: {coin.exchange_source}"
            f" | وضعیت بازار: {tradeable}"
        )
        lines.append(
            f"  تایم‌فریم‌ها: [{tf_str}] | هم‌جهتی: {coin.mtf_alignment}"
            f" | Funding: {funding} | Spread: {spread}"
        )
        if coin.data_issues:
            lines.append(
                f"  هشدار داده: {' | '.join(str(x) for x in coin.data_issues)}"
            )
        lines.extend(_format_direction_block(coin.long_setup))
        lines.extend(_format_direction_block(coin.short_setup))

    lines.extend([
        "",
        "============================================================",
    ])
    return "\n".join(lines)


def save_reports(report: ScanReport, output_dir: str) -> Tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "scan_report.json")
    txt_path = os.path.join(output_dir, "scan_report.txt")

    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, ensure_ascii=False, indent=2)

    with open(txt_path, "w", encoding="utf-8") as file:
        file.write(format_text_report(report))

    return json_path, txt_path


def _split_telegram_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> List[str]:
    """Split a Telegram HTML message without cutting a line in the middle."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # A single unusually long line is split as a last resort.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current:
        chunks.append(current)
    return chunks


def send_telegram_report(report: ScanReport, bot_token: str, chat_id: str) -> bool:
    if not bot_token or not chat_id:
        logger.info("Telegram credentials not provided; skipping Telegram dispatch.")
        return False

    conditional_long = sum(
        1 for coin in report.coins if coin.long_setup.status == "CONDITIONAL"
    )
    conditional_short = sum(
        1 for coin in report.coins if coin.short_setup.status == "CONDITIONAL"
    )

    lines = [
        "📊 <b>گزارش اسکنر فیوچرز (Perpetual SMC)</b>",
        f"🕒 زمان: <code>{_safe(report.generated_at_utc)}</code>",
        f"🛡 ریسک هدف: <b>{_safe(report.risk_per_trade_pct)}%</b> | اجرای سفارش: <b>غیرفعال</b>",
        (
            f"✅ داده کافی: {report.valid_coins}/{report.total_coins}"
            f" | 🟢 LONG تأییدشده: {report.active_long_signals}"
            f" | 🔴 SHORT تأییدشده: {report.active_short_signals}"
        ),
        f"🟡 LONG مشروط: {conditional_long} | SHORT مشروط: {conditional_short}",
        "<i>امتیاز همگرایی احتمال برد نیست.</i>",
    ]

    for coin in report.coins:
        for setup in (coin.long_setup, coin.short_setup):
            status = str(setup.status)
            if status not in ("CONFIRMED", "CONDITIONAL", "SIGNAL"):
                continue

            lines.extend([
                "",
                (
                    f"<b>{_safe(coin.symbol)}USDT ({_safe(setup.direction)})"
                    f" — {_safe(_status_label(status))}</b>"
                ),
                (
                    f"ورود: <code>{_safe(_format_number(setup.entry_low))}"
                    f" - {_safe(_format_number(setup.entry_high))}</code>"
                    f" | SL: <code>{_safe(_format_number(setup.stop_loss))}</code>"
                ),
                (
                    f"TP1: <code>{_safe(_format_number(setup.target_1))}</code>"
                    f" | TP2: <code>{_safe(_format_number(setup.target_2))}</code>"
                    f" | RR: {_safe(_format_number(setup.rr_tp1, 2))}"
                    f"/{_safe(_format_number(setup.rr_tp2, 2))}"
                    f" | امتیاز: {_safe(setup.confluence_score)}"
                ),
            ])
            if setup.activation_conditions:
                lines.append(
                    "فعال‌سازی: " + _safe("؛ ".join(setup.activation_conditions[:2]))
                )

    if not any(
        setup.status in ("CONFIRMED", "CONDITIONAL", "SIGNAL")
        for coin in report.coins
        for setup in (coin.long_setup, coin.short_setup)
    ):
        lines.extend(["", "⚪️ در این اسکن ستاپ تأییدشده یا مشروطی وجود ندارد."])

    if report.system_errors:
        lines.extend([
            "",
            "<b>خطاهای سیستمی:</b>",
            *[f"• {_safe(error)}" for error in report.system_errors[:5]],
        ])

    messages = _split_telegram_message("\n".join(lines))
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    all_sent = True
    for message in messages:
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok", False):
                all_sent = False
                logger.error("Telegram API returned ok=false.")
        except Exception as exc:
            all_sent = False
            # Do not log the request URL; it contains the bot token.
            logger.error("Failed to send Telegram report: %s", exc)

    return all_sent
