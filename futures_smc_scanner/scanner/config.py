from dataclasses import dataclass, field
import os
from typing import List


WATCHLIST_20: List[str] = [
    "BTC", "ETH", "SOL", "XRP", "BNB",
    "DOGE", "ADA", "AVAX", "LINK", "LTC",
    "HYPE", "SUI", "AAVE", "UNI", "NEAR",
    "ICP", "RENDER", "ONDO", "PYTH", "JUP",
]

SUPPORTED_TIMEFRAMES: List[str] = ["15m", "1h", "4h"]

TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


@dataclass(frozen=True)
class ScannerConfig:
    watchlist: List[str] = field(default_factory=lambda: list(WATCHLIST_20))
    timeframes: List[str] = field(default_factory=lambda: list(SUPPORTED_TIMEFRAMES))
    binance_base_url: str = "https://fapi.binance.com"
    bybit_base_url: str = "https://api.bybit.com"
    request_timeout: float = 10.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0
    candle_limit: int = 260
    min_candles_required: int = 210
    risk_per_trade_pct: float = 0.5
    account_equity_usdt: float = 10000.0
    live_trading_enabled: bool = False  # Always disabled; zero order placement
    max_spread_pct: float = 0.15
    max_abs_funding_rate: float = 0.0015
    min_24h_quote_volume_usdt: float = 5_000_000.0
    min_rr_tp1: float = 1.5
    min_rr_tp2: float = 2.5
    min_confluence_score: float = 65.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    output_dir: str = "output"

    @classmethod
    def from_env(cls, output_dir: str | None = None) -> "ScannerConfig":
        raw_tfs = os.getenv("SCAN_TIMEFRAMES", "15m,1h,4h")
        parsed_tfs = [tf.strip() for tf in raw_tfs.split(",") if tf.strip() in SUPPORTED_TIMEFRAMES]
        if not parsed_tfs:
            parsed_tfs = list(SUPPORTED_TIMEFRAMES)

        # Explicitly enforce live_trading_enabled = False
        _ = os.getenv("LIVE_TRADING_ENABLED", "false")
        live_trading_enabled = False

        equity_raw = os.getenv("ACCOUNT_EQUITY_USDT", "10000.0")
        try:
            equity = float(equity_raw)
            if equity <= 0:
                equity = 10000.0
        except ValueError:
            equity = 10000.0

        risk_raw = os.getenv("RISK_PER_TRADE_PCT", "0.5")
        try:
            risk = float(risk_raw)
            if risk <= 0 or risk > 0.5:
                risk = 0.5
        except ValueError:
            risk = 0.5

        spread_raw = os.getenv("MAX_SPREAD_PCT", "0.15")
        try:
            max_spread = max(0.001, float(spread_raw))
        except ValueError:
            max_spread = 0.15

        funding_raw = os.getenv("MAX_ABS_FUNDING_RATE", "0.0015")
        try:
            max_funding = max(0.0001, float(funding_raw))
        except ValueError:
            max_funding = 0.0015

        min_candles_raw = os.getenv("MIN_CANDLES_REQUIRED", "210")
        try:
            min_candles = max(60, int(min_candles_raw))
        except ValueError:
            min_candles = 210

        return cls(
            watchlist=list(WATCHLIST_20),
            timeframes=parsed_tfs,
            min_candles_required=min_candles,
            risk_per_trade_pct=risk,
            account_equity_usdt=equity,
            live_trading_enabled=live_trading_enabled,
            max_spread_pct=max_spread,
            max_abs_funding_rate=max_funding,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            output_dir=output_dir or os.getenv("OUTPUT_DIR", "output").strip() or "output",
        )
