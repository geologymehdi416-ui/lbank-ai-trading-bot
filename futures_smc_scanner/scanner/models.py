from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    is_closed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MarketDerivativeContext:
    symbol: str
    exchange_source: str
    mark_price: float
    index_price: Optional[float]
    funding_rate: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread_pct: Optional[float]
    quote_volume_24h: Optional[float]
    fetched_at_ms: int
    is_real_perpetual: bool = True
    warnings: List[str] = field(default_factory=list)

    # Independent data timestamps
    price_fetched_at_ms: Optional[int] = None
    funding_fetched_at_ms: Optional[int] = None
    orderbook_fetched_at_ms: Optional[int] = None
    ticker_fetched_at_ms: Optional[int] = None

    # Source tracking for supplemental derivatives data
    candle_exchange_source: Optional[str] = None
    context_exchange_source: Optional[str] = None
    lbank_mark_price: Optional[float] = None
    lbank_funding_rate: Optional[float] = None
    lbank_best_bid: Optional[float] = None
    lbank_best_ask: Optional[float] = None
    lbank_quote_volume_24h: Optional[float] = None
    lbank_fetched_at_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DirectionSetup:
    direction: str
    status: str
    entry_low: Optional[float]
    entry_high: Optional[float]
    entry_reference: Optional[float]
    stop_loss: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    rr_tp1: Optional[float]
    rr_tp2: Optional[float]
    suggested_risk_pct: float
    suggested_risk_usdt: float
    suggested_position_size_base: Optional[float]
    confluence_score: float
    rationale: List[str]
    activation_conditions: List[str]
    invalidation_conditions: List[str]
    no_trade_reasons: List[str] = field(default_factory=list)

    # Setup lifecycle
    created_at_ms: Optional[int] = None
    expires_at_ms: Optional[int] = None
    last_validated_at_ms: Optional[int] = None
    setup_invalidated: bool = False

    # SMC metadata
    structure_state: Optional[str] = None
    liquidity_sweep_type: Optional[str] = None
    order_block_low: Optional[float] = None
    order_block_high: Optional[float] = None
    fvg_low: Optional[float] = None
    fvg_high: Optional[float] = None
    market_zone: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CoinScanResult:
    symbol: str
    exchange_source: str
    market_tradeable: bool
    data_valid: bool
    timeframe_bias: Dict[str, str]
    mtf_alignment: str
    funding_rate: Optional[float]
    spread_pct: Optional[float]
    long_setup: DirectionSetup
    short_setup: DirectionSetup
    data_issues: List[str] = field(default_factory=list)

    # Separate data quality states
    data_sufficient: bool = True
    analysis_status: str = "AVAILABLE"

    # Data provenance
    candle_exchange_source: Optional[str] = None
    context_exchange_source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange_source": self.exchange_source,
            "candle_exchange_source": self.candle_exchange_source,
            "context_exchange_source": self.context_exchange_source,
            "market_tradeable": self.market_tradeable,
            "data_valid": self.data_valid,
            "data_sufficient": self.data_sufficient,
            "analysis_status": self.analysis_status,
            "timeframe_bias": self.timeframe_bias,
            "mtf_alignment": self.mtf_alignment,
            "funding_rate": self.funding_rate,
            "spread_pct": self.spread_pct,
            "long": self.long_setup.to_dict(),
            "short": self.short_setup.to_dict(),
            "data_issues": self.data_issues,
        }


@dataclass
class ScanReport:
    generated_at_utc: str
    generated_at_ms: int
    primary_exchange: str
    fallback_exchange: str
    live_trading_enabled: bool
    risk_per_trade_pct: float
    account_equity_usdt: float
    total_coins: int
    valid_coins: int
    active_long_signals: int
    active_short_signals: int
    coins: List[CoinScanResult]
    system_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": {
                "generated_at_utc": self.generated_at_utc,
                "generated_at_ms": self.generated_at_ms,
                "primary_exchange": self.primary_exchange,
                "fallback_exchange": self.fallback_exchange,
                "live_trading_enabled": self.live_trading_enabled,
                "risk_per_trade_pct": self.risk_per_trade_pct,
                "account_equity_usdt": self.account_equity_usdt,
                "total_coins": self.total_coins,
                "valid_coins": self.valid_coins,
                "active_long_signals": self.active_long_signals,
                "active_short_signals": self.active_short_signals,
                "system_errors": self.system_errors,
            },
            "coins": [coin.to_dict() for coin in self.coins],
        }