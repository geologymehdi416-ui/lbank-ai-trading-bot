
from dataclasses import dataclass, field
import os
from typing import List, Tuple


WATCHLIST_20: Tuple[str, ...] = (
    "BTC", "ETH", "SOL", "XRP", "BNB",
    "DOGE", "ADA", "AVAX", "LINK", "LTC",
    "HYPE", "SUI", "AAVE", "UNI", "NEAR",
    "ICP", "RENDER", "ONDO", "PYTH", "JUP",
)

SUPPORTED_TIMEFRAMES: Tuple[str, ...] = ("15m", "1h", "4h")

TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


def _env_float(name: str, default: float, minimum: float,
               maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not minimum <= value <= maximum:
        return default
    return value


def _env_int(name: str, default: int, minimum: int,
             maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not minimum <= value <= maximum:
        return default
    return value


@dataclass(frozen=True)
class ScannerConfig:
    watchlist: Tuple[str, ...] = WATCHLIST_20
    timeframes: Tuple[str, ...] = SUPPORTED_TIMEFRAMES

    binance_base_url: str = "https://fapi.binance.com"
    bybit_base_url: str = "https://api.bybit.com"

    request_timeout: float = 10.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0

    candle_limit: int = 260
    min_candles_required: int = 210

    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 1.0
    max_total_open_risk_pct: float = 2.0
    max_open_positions: int = 3
    suggested_leverage_min: int = 2
    suggested_leverage_max: int = 3

    account_equity_usdt: float = 10000.0

    # Analysis only: order execution is permanently disabled.
    live_trading_enabled: bool = False

    max_spread_pct: float = 0.15
    max_abs_funding_rate: float = 0.0015
    min_24h_quote_volume_usdt: float = 5_000_000.0

    min_rr_tp1: float = 1.5
    min_rr_tp2: float = 2.5
    min_confluence_score: float = 65.0

    max_market_data_age_ms: int = 15000
    max_orderbook_age_ms: int = 5000
    max_candle_gap_count: int = 0
    setup_validity_minutes: int = 60

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    output_dir: str = "output"

    @classmethod
    def from_env(cls, output_dir: str | None = None) -> "ScannerConfig":

        raw_tfs = os.getenv("SCAN_TIMEFRAMES", "15m,1h,4h")
        parsed_tfs = tuple(
            tf.strip()
            for tf in raw_tfs.split(",")
            if tf.strip() in SUPPORTED_TIMEFRAMES
        )

        if not parsed_tfs:
            parsed_tfs = SUPPORTED_TIMEFRAMES

        # Enforce the exact approved watchlist.
        watchlist = WATCHLIST_20

        # Never enable order execution through environment variables.
        live_trading_enabled = False

        equity = _env_float(
            "ACCOUNT_EQUITY_USDT", 10000.0, 1.0, 1000000000.0
        )

        risk = _env_float(
            "RISK_PER_TRADE_PCT", 0.5, 0.01, 0.5
        )

        max_spread = _env_float(
            "MAX_SPREAD_PCT", 0.15, 0.001, 2.0
        )

        max_funding = _env_float(
            "MAX_ABS_FUNDING_RATE", 0.0015, 0.0001, 0.05
        )

        min_volume = _env_float(
            "MIN_24H_QUOTE_VOLUME_USDT",
            5000000.0, 0.0, 1000000000000.0
        )

        min_candles = _env_int(
            "MIN_CANDLES_REQUIRED", 210, 210, 1000
        )

        candle_limit = _env_int(
            "CANDLE_LIMIT", 260, min_candles, 1500
        )

        timeout = _env_float(
            "REQUEST_TIMEOUT", 10.0, 2.0, 60.0
        )

        retries = _env_int(
            "MAX_RETRIES", 3, 1, 10
        )

        retry_backoff = _env_float(
            "RETRY_BACKOFF_BASE", 1.0, 0.1, 10.0
        )

        return cls(
            watchlist=watchlist,
            timeframes=parsed_tfs,
            candle_limit=candle_limit,
            min_candles_required=min_candles,
            request_timeout=timeout,
            max_retries=retries,
            retry_backoff_base=retry_backoff,
            risk_per_trade_pct=risk,
            account_equity_usdt=equity,
            live_trading_enabled=live_trading_enabled,
            max_spread_pct=max_spread,
            max_abs_funding_rate=max_funding,
            min_24h_quote_volume_usdt=min_volume,
            telegram_bot_token=os.getenv(
                "TELEGRAM_BOT_TOKEN", ""
            ).strip(),
            telegram_chat_id=os.getenv(
                "TELEGRAM_CHAT_ID", ""
            ).strip(),
            output_dir=(
                output_dir
                or os.getenv("OUTPUT_DIR", "output").strip()
                or "output"
            ),
            max_market_data_age_ms=_env_int(
                "MAX_MARKET_DATA_AGE_MS", 15000, 1000, 300000
            ),
            max_orderbook_age_ms=_env_int(
                "MAX_ORDERBOOK_AGE_MS", 5000, 500, 60000
            ),
            max_candle_gap_count=_env_int(
                "MAX_CANDLE_GAP_COUNT", 0, 0, 100
            ),
            setup_validity_minutes=_env_int(
                "SETUP_VALIDITY_MINUTES", 60, 5, 1440
            ),
        )
