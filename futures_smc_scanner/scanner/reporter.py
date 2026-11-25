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
    return html.escape(str(value), quote=False)


def _format_number(value, precision: int = 8) -> str:
    if value is None:
        return "N/A"

    try:
        number = float(value)

        if number != number or number in (
            float("inf"),
            float("-inf"),
        ):
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
        "SIGNAL": "سیگنال",
    }

    return labels.get(str(status), str(status))


def _format_direction_block(
    setup: DirectionSetup,
) -> List[str]:
    lines: List[str] = []
    status = str(setup.status)

    lines.append(
        f"  [{setup.direction}] وضعیت: {_status_label(status)}"
        f" | امتیاز همگرایی: {setup.confluence_score}/100"
    )

    if status in ("NO_TRADE", "INSUFFICIENT_DATA"):
        lines.append(
            "    - ورود / حد ضرر / تارگت: ارائه نمی‌شود"
        )

        if setup.no_trade_reasons:
            lines.append(
                "    - دلیل: "
                + " | ".join(
                    str(item)
                    for item in setup.no_trade_reasons
                )
            )

    else:
        entry_str = (
            f"{_format_number(setup.entry_low)} - "
            f"{_format_number(setup.entry_high)}"
            if (
                setup.entry_low is not None
                and setup.entry_high is not None
            )
            else "N/A"
        )

        lines.append(
            f"    - محدوده ورود: {entry_str}"
            f" (مرجع: {_format_number(setup.entry_reference)})"
        )

        lines.append(
            f"    - حد ضرر: {_format_number(setup.stop_loss)}"
        )

        lines.append(
            f"    - تارگت ۱: {_format_number(setup.target_1)}"
            f" | تارگت ۲: {_format_number(setup.target_2)}"
        )

        lines.append(
            f"    - Risk/Reward: "
            f"TP1={_format_number(setup.rr_tp1, 2)}R"
            f" | TP2={_format_number(setup.rr_tp2, 2)}R"
        )

        lines.append(
            "    - اندازه موقعیت: صرفاً محاسباتی؛ "
            "اجرای سفارش غیرفعال است"
        )

    if setup.rationale:
        lines.append(
            "    - تحلیل: "
            + "؛ ".join(
                str(item) for item in setup.rationale[:4]
            )
        )

    if setup.activation_conditions:
        lines.append(
            "    - شرایط فعال‌شدن: "
            + "؛ ".join(
                str(item)
                for item in setup.activation_conditions
            )
        )

    if setup.invalidation_conditions:
        lines.append(
            "    - شرایط باطل‌شدن: "
            + "؛ ".join(
                str(item)
                for item in setup.invalidation_conditions
            )
        )

    return lines


def _coin_sources(coin: CoinScanResult) -> Tuple[str, str]:
    candle_source = (
        getattr(coin, "candle_exchange_source", None)
        or coin.exchange_source
    )

    context_source = (
        getattr(coin, "context_exchange_source", None)
        or coin.exchange_source
    )

    return str(candle_source), str(context_source)


def build_scan_report(
    coins: List[CoinScanResult],
    config: ScannerConfig,
    now_ms: int,
    system_errors: Optional[List[str]] = None,
) -> ScanReport:
    dt_utc = datetime.fromtimestamp(
        now_ms / 1000.0,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    valid_coins = sum(
        1
        for coin in coins
        if getattr(
            coin,
            "data_sufficient",
            getattr(coin, "data_valid", False),
        )
    )

    active_long = sum(
        1
        for coin in coins
        if coin.long_setup.status in ("CONFIRMED", "SIGNAL")
    )

    active_short = sum(
        1
        for coin in coins
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
        1
        for coin in report.coins
        if coin.long_setup.status == "CONDITIONAL"
    )

    conditional_short = sum(
        1
        for coin in report.coins
        if coin.short_setup.status == "CONDITIONAL"
    )

    insufficient = sum(
        1
        for coin in report.coins
        if (
            coin.long_setup.status == "INSUFFICIENT_DATA"
            or coin.short_setup.status == "INSUFFICIENT_DATA"
        )
    )

    lines: List[str] = [
        "============================================================",
        "گزارش اسکنر فیوچرز ارز دیجیتال (Perpetual SMC & Multi-TF)",
        f"زمان تولید (UTC): {report.generated_at_utc}",
        (
            f"منبع اصلی: {report.primary_exchange}"
            f" | پشتیبان: {report.fallback_exchange}"
        ),
        (
            f"ریسک هدف: {report.risk_per_trade_pct}%"
            " | معاملات واقعی: غیرفعال (فقط تحلیل)"
        ),
        (
            f"تعداد ارزها: {report.total_coins}"
            f" | داده کافی: {report.valid_coins}"
            f" | LONG تأییدشده: {report.active_long_signals}"
            f" | SHORT تأییدشده: {report.active_short_signals}"
        ),
        (
            f"LONG مشروط: {conditional_long}"
            f" | SHORT مشروط: {conditional_short}"
            f" | موارد دارای داده ناکافی: {insufficient}"
        ),
        (
            "توجه: امتیاز همگرایی احتمال برد نیست؛ "
            "این گزارش سفارش معاملاتی ارسال نمی‌کند."
        ),
        "============================================================",
    ]

    if report.system_errors:
        lines.append("خطاهای سیستمی:")

        for error in report.system_errors:
            lines.append(f"  * {error}")

        lines.append(
            "------------------------------------------------------------"
        )

    for coin in report.coins:
        tf_str = ", ".join(
            f"{key}:{value}"
            for key, value in coin.timeframe_bias.items()
        )

        funding = (
            f"{coin.funding_rate:.6f}"
            if coin.funding_rate is not None
            else "دریافت نشد"
        )

        spread = (
            f"{coin.spread_pct:.4f}%"
            if coin.spread_pct is not None
            else "N/A"
        )

        tradeable = (
            "قابل بررسی"
            if coin.market_tradeable
            else "غیرقابل معامله / NO TRADE"
        )

        candle_source, context_source = _coin_sources(coin)

        lines.append("")
        lines.append(
            f"ارز: {coin.symbol}USDT"
            f" | وضعیت بازار: {tradeable}"
        )

        lines.append(
            f"  منبع کندل: {candle_source}"
            f" | منبع اطلاعات مشتقه: {context_source}"
        )

        lines.append(
            f"  تایم‌فریم‌ها: [{tf_str}]"
            f" | هم‌جهتی: {coin.mtf_alignment}"
            f" | Funding: {funding}"
            f" | Spread: {spread}"
        )

        if coin.data_issues:
            lines.append(
                "  هشدار داده: "
                + " | ".join(
                    str(item)
                    for item in coin.data_issues
                )
            )

        lines.extend(
            _format_direction_block(coin.long_setup)
        )

        lines.extend(
            _format_direction_block(coin.short_setup)
        )

    lines.extend([
        "",
        "============================================================",
    ])

    return "\n".join(lines)


def save_reports(
    report: ScanReport,
    output_dir: str,
) -> Tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(
        output_dir,
        "scan_report.json",
    )

    txt_path = os.path.join(
        output_dir,
        "scan_report.txt",
    )

    with open(
        json_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            report.to_dict(),
            file,
            ensure_ascii=False,
            indent=2,
        )

    with open(
        txt_path,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(
            format_text_report(report)
        )

    return json_path, txt_path


# ---------------- TELEGRAM HTML MESSAGE SPLITTING ----------------

def _split_telegram_message(
    text: str,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> List[str]:
    """
    Split Telegram HTML without cutting tags or entities.

    HTML tags are tracked and reopened in the next chunk.
    """

    if len(text) <= limit:
        return [text]

    import re

    token_pattern = re.compile(
        r"(<[^>]+>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);)"
        r"|([^<&]+|[<&])"
    )

    chunks: List[str] = []
    current = ""
    open_tags: List[str] = []

    tag_name_pattern = re.compile(
        r"^<\s*(/?)\s*([a-zA-Z0-9]+)"
    )

    void_tags = {
        "br",
    }

    def closing_tags() -> str:
        return "".join(
            f"</{tag}>" for tag in reversed(open_tags)
        )

    def opening_tags() -> str:
        return "".join(
            f"<{tag}>" for tag in open_tags
        )

    def flush() -> None:
        nonlocal current

        if not current:
            return

        chunk = current + closing_tags()

        if chunk.strip():
            chunks.append(chunk)

        current = opening_tags()

    tokens = token_pattern.findall(text)

    for tag_token, text_token in tokens:
        token = tag_token or text_token

        if not token:
            continue

        if token.startswith("<"):
            match = tag_name_pattern.match(token)

            if match:
                is_closing = bool(match.group(1))
                tag_name = match.group(2).lower()

                if tag_name not in void_tags:
                    if is_closing:
                        if tag_name in open_tags:
                            index = len(open_tags) - 1 - (
                                open_tags[::-1].index(tag_name)
                            )
                            open_tags = open_tags[:index]
                    else:
                        open_tags.append(tag_name)

        candidate = current + token + closing_tags()

        if len(candidate) > limit and current:
            flush()

            candidate = current + token + closing_tags()

        if len(candidate) > limit:
            # A single token is too large. Do not send malformed HTML.
            logger.warning(
                "Telegram message token exceeds configured limit."
            )
            continue

        current += token

    if current:
        final_chunk = current + closing_tags()

        if final_chunk.strip():
            chunks.append(final_chunk)

    return chunks


def send_telegram_report(
    report: ScanReport,
    bot_token: str,
    chat_id: str,
) -> bool:
    if not bot_token or not chat_id:
        logger.info(
            "Telegram credentials not provided; "
            "skipping Telegram dispatch."
        )
        return False

    conditional_long = sum(
        1
        for coin in report.coins
        if coin.long_setup.status == "CONDITIONAL"
    )

    conditional_short = sum(
        1
        for coin in report.coins
        if coin.short_setup.status == "CONDITIONAL"
    )

    lines = [
        "📊 <b>گزارش اسکنر فیوچرز (Perpetual SMC)</b>",
        (
            f"🕒 زمان: "
            f"<code>{_safe(report.generated_at_utc)}</code>"
        ),
        (
            f"🛡 ریسک هدف: "
            f"<b>{_safe(report.risk_per_trade_pct)}%</b>"
            " | اجرای سفارش: <b>غیرفعال</b>"
        ),
        (
            f"✅ داده کافی: "
            f"{report.valid_coins}/{report.total_coins}"
            f" | 🟢 LONG تأییدشده: "
            f"{report.active_long_signals}"
            f" | 🔴 SHORT تأییدشده: "
            f"{report.active_short_signals}"
        ),
        (
            f"🟡 LONG مشروط: {conditional_long}"
            f" | SHORT مشروط: {conditional_short}"
        ),
        "<i>امتیاز همگرایی احتمال برد نیست.</i>",
    ]

    has_signal = False

    for coin in report.coins:
        candle_source, context_source = _coin_sources(coin)

        for setup in (
            coin.long_setup,
            coin.short_setup,
        ):
            status = str(setup.status)

            if status not in (
                "CONFIRMED",
                "CONDITIONAL",
                "SIGNAL",
            ):
                continue

            has_signal = True

            lines.extend([
                "",
                (
                    f"<b>{_safe(coin.symbol)}USDT "
                    f"({ _safe(setup.direction) })"
                    f" — {_safe(_status_label(status))}</b>"
                ),
                (
                    f"کندل: {_safe(candle_source)}"
                    f" | مشتقه: {_safe(context_source)}"
                ),
                (
                    f"ورود: <code>"
                    f"{_safe(_format_number(setup.entry_low))}"
                    f" - "
                    f"{_safe(_format_number(setup.entry_high))}"
                    f"</code>"
                    f" | SL: <code>"
                    f"{_safe(_format_number(setup.stop_loss))}"
                    f"</code>"
                ),
                (
                    f"TP1: <code>"
                    f"{_safe(_format_number(setup.target_1))}"
                    f"</code>"
                    f" | TP2: <code>"
                    f"{_safe(_format_number(setup.target_2))}"
                    f"</code>"
                    f" | RR: "
                    f"{_safe(_format_number(setup.rr_tp1, 2))}"
                    f"/"
                    f"{_safe(_format_number(setup.rr_tp2, 2))}"
                    f" | امتیاز: "
                    f"{_safe(setup.confluence_score)}"
                ),
            ])

            if setup.activation_conditions:
                lines.append(
                    "فعال‌سازی: "
                    + _safe(
                        "؛ ".join(
                            setup.activation_conditions[:2]
                        )
                    )
                )

            if setup.no_trade_reasons:
                lines.append(
                    "دلایل محدودیت: "
                    + _safe(
                        "؛ ".join(
                            setup.no_trade_reasons[:2]
                        )
                    )
                )

        if coin.data_issues:
            lines.append(
                f"⚠️ {_safe(coin.symbol)}: "
                + _safe(
                    "؛ ".join(coin.data_issues[:3])
                )
            )

    if not has_signal:
        lines.extend([
            "",
            "⚪️ در این اسکن ستاپ تأییدشده یا مشروطی وجود ندارد.",
        ])

    if report.system_errors:
        lines.extend([
            "",
            "<b>خطاهای سیستمی:</b>",
            *[
                f"• {_safe(error)}"
                for error in report.system_errors[:5]
            ],
        ])

    messages = _split_telegram_message(
        "\n".join(lines)
    )

    url = (
        f"https://api.telegram.org/bot"
        f"{bot_token}/sendMessage"
    )

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
                logger.error(
                    "Telegram API returned ok=false."
                )

        except Exception as exc:
            all_sent = False

            # Never log the URL because it contains the bot token.
            logger.error(
                "Failed to send Telegram report: %s",
                exc,
            )

    return all_sent