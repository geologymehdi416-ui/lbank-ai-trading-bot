import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import requests

from .config import ScannerConfig, TIMEFRAME_MS
from .models import Candle, MarketDerivativeContext

logger = logging.getLogger(__name__)


class ExchangeDataError(Exception):
    """Raised when real perpetual market data cannot be retrieved or validated."""


class PerpetualDataClient:
    """Fetches real Perpetual Futures data from Binance USDⓈ-M (primary) and Bybit Linear (fallback).

    Strictly forbids Spot endpoints and never fabricates synthetic market data.
    Filters out unclosed / incomplete candles using current timestamp and candle close time.
    """

    BYBIT_INTERVAL_MAP = {
        "15m": "15",
        "1h": "60",
        "4h": "240",
    }

    def __init__(
        self,
        config: ScannerConfig,
        http_get: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        clock_ms_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self.config = config
        self._http_get = http_get or self._default_http_get
        self._clock_ms_fn = clock_ms_fn or (lambda: int(time.time() * 1000))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PerpetualSMCSymbolScanner/1.0"})

    def _default_http_get(self, url: str, params: Dict[str, Any]) -> Any:
        # Guard against accidentally calling Spot URLs
        lowered = url.lower()
        if "/api/v3/" in lowered or "api.binance.com/api" in lowered or "/spot/" in lowered:
            raise ExchangeDataError(f"Spot endpoint access is forbidden: {url}")

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self.config.request_timeout)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_sec = float(retry_after) if retry_after and retry_after.isdigit() else self.config.retry_backoff_base * (2 ** attempt)
                    logger.warning("Rate limited (429) on %s; sleeping %.2fs (attempt %d)", url, sleep_sec, attempt)
                    time.sleep(min(sleep_sec, 15.0))
                    continue
                if resp.status_code >= 500:
                    sleep_sec = self.config.retry_backoff_base * (2 ** (attempt - 1))
                    logger.warning("Server error (%d) on %s; retrying in %.2fs", resp.status_code, url, sleep_sec)
                    time.sleep(sleep_sec)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    sleep_sec = self.config.retry_backoff_base * (2 ** (attempt - 1))
                    time.sleep(sleep_sec)
        raise ExchangeDataError(f"Failed request to {url} after {self.config.max_retries} attempts: {last_exc}")

    @staticmethod
    def validate_and_filter_candles(
        candles: List[Candle],
        timeframe: str,
        now_ms: int,
        min_required: int,
    ) -> Tuple[List[Candle], List[str]]:
        """Removes unclosed/incomplete candles and validates monotonic timestamps and positive OHLCV."""
        issues: List[str] = []
        if timeframe not in TIMEFRAME_MS:
            return [], [f"Unsupported timeframe: {timeframe}"]

        tf_ms = TIMEFRAME_MS[timeframe]
        closed_candles: List[Candle] = []

        for c in candles:
            # A candle is incomplete if explicit is_closed is False or close_time_ms > now_ms
            if not c.is_closed or c.close_time_ms > now_ms:
                continue
            if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
                issues.append(f"{timeframe}: non-positive OHLC at {c.open_time_ms}")
                continue
            if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
                issues.append(f"{timeframe}: inconsistent high/low bounds at {c.open_time_ms}")
                continue
            if c.volume < 0:
                issues.append(f"{timeframe}: negative volume at {c.open_time_ms}")
                continue
            closed_candles.append(c)

        closed_candles.sort(key=lambda x: x.open_time_ms)

        # Deduplicate by open_time_ms
        deduped: List[Candle] = []
        seen_times = set()
        for c in closed_candles:
            if c.open_time_ms in seen_times:
                continue
            seen_times.add(c.open_time_ms)
            deduped.append(c)

        # Check for large timestamp gaps
        gaps = 0
        for i in range(1, len(deduped)):
            delta = deduped[i].open_time_ms - deduped[i - 1].open_time_ms
            if delta > tf_ms * 2:
                gaps += 1
        if gaps > 0:
            issues.append(f"{timeframe}: detected {gaps} candle gap(s)")

        if len(deduped) < min_required:
            issues.append(f"{timeframe}: only {len(deduped)} closed candles (< {min_required} required)")

        return deduped, issues

    # ---------------- BINANCE USDⓈ-M FUTURES ----------------

    def _fetch_binance_symbol(self, symbol: str, timeframes: List[str], now_ms: int) -> Tuple[MarketDerivativeContext, Dict[str, List[Candle]], List[str]]:
        pair = f"{symbol}USDT"
        issues: List[str] = []

        prem_data = self._http_get(f"{self.config.binance_base_url}/fapi/v1/premiumIndex", {"symbol": pair})
        book_data = self._http_get(f"{self.config.binance_base_url}/fapi/v1/ticker/bookTicker", {"symbol": pair})
        ticker_24h = self._http_get(f"{self.config.binance_base_url}/fapi/v1/ticker/24hr", {"symbol": pair})

        mark_price = float(prem_data["markPrice"])
        index_price = float(prem_data["indexPrice"]) if prem_data.get("indexPrice") is not None else None
        funding_rate = float(prem_data["lastFundingRate"]) if prem_data.get("lastFundingRate") is not None else None

        best_bid = float(book_data["bidPrice"]) if book_data.get("bidPrice") is not None else None
        best_ask = float(book_data["askPrice"]) if book_data.get("askPrice") is not None else None
        spread_pct: Optional[float] = None
        if best_bid and best_ask and best_bid > 0 and best_ask >= best_bid:
            mid = (best_bid + best_ask) / 2.0
            spread_pct = ((best_ask - best_bid) / mid) * 100.0

        quote_vol_24h = float(ticker_24h["quoteVolume"]) if ticker_24h.get("quoteVolume") is not None else None

        if mark_price <= 0:
            raise ExchangeDataError(f"Binance invalid markPrice for {pair}")

        ctx = MarketDerivativeContext(
            symbol=symbol,
            exchange_source="binance_usds_m_perpetual",
            mark_price=mark_price,
            index_price=index_price,
            funding_rate=funding_rate,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=spread_pct,
            quote_volume_24h=quote_vol_24h,
            fetched_at_ms=now_ms,
            is_real_perpetual=True,
        )

        tf_candles: Dict[str, List[Candle]] = {}
        for tf in timeframes:
            raw_klines = self._http_get(
                f"{self.config.binance_base_url}/fapi/v1/klines",
                {"symbol": pair, "interval": tf, "limit": self.config.candle_limit},
            )
            parsed: List[Candle] = []
            for row in raw_klines:
                open_t = int(row[0])
                close_t = int(row[6])
                parsed.append(
                    Candle(
                        open_time_ms=open_t,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        close_time_ms=close_t,
                        is_closed=(close_t <= now_ms),
                    )
                )
            valid_c, c_issues = self.validate_and_filter_candles(parsed, tf, now_ms, self.config.min_candles_required)
            tf_candles[tf] = valid_c
            issues.extend(c_issues)

        return ctx, tf_candles, issues

    # ---------------- BYBIT USDT LINEAR PERPETUAL ----------------

    def _fetch_bybit_symbol(self, symbol: str, timeframes: List[str], now_ms: int) -> Tuple[MarketDerivativeContext, Dict[str, List[Candle]], List[str]]:
        pair = f"{symbol}USDT"
        issues: List[str] = []

        ticker_resp = self._http_get(
            f"{self.config.bybit_base_url}/v5/market/tickers",
            {"category": "linear", "symbol": pair},
        )
        items = ticker_resp.get("result", {}).get("list", []) if isinstance(ticker_resp, dict) else []
        if not items:
            raise ExchangeDataError(f"Bybit empty ticker list for {pair}")
        t0 = items[0]

        mark_price = float(t0.get("markPrice") or t0.get("lastPrice") or 0.0)
        index_price = float(t0["indexPrice"]) if t0.get("indexPrice") else None
        funding_rate = float(t0["fundingRate"]) if t0.get("fundingRate") not in (None, "") else None
        best_bid = float(t0["bid1Price"]) if t0.get("bid1Price") else None
        best_ask = float(t0["ask1Price"]) if t0.get("ask1Price") else None
        quote_vol_24h = float(t0["turnover24h"]) if t0.get("turnover24h") else None

        spread_pct: Optional[float] = None
        if best_bid and best_ask and best_bid > 0 and best_ask >= best_bid:
            mid = (best_bid + best_ask) / 2.0
            spread_pct = ((best_ask - best_bid) / mid) * 100.0

        if mark_price <= 0:
            raise ExchangeDataError(f"Bybit invalid markPrice for {pair}")

        ctx = MarketDerivativeContext(
            symbol=symbol,
            exchange_source="bybit_usdt_linear_perpetual",
            mark_price=mark_price,
            index_price=index_price,
            funding_rate=funding_rate,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=spread_pct,
            quote_volume_24h=quote_vol_24h,
            fetched_at_ms=now_ms,
            is_real_perpetual=True,
        )

        tf_candles: Dict[str, List[Candle]] = {}
        for tf in timeframes:
            bybit_iv = self.BYBIT_INTERVAL_MAP.get(tf)
            if not bybit_iv:
                issues.append(f"Bybit unsupported timeframe: {tf}")
                tf_candles[tf] = []
                continue
            kline_resp = self._http_get(
                f"{self.config.bybit_base_url}/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": pair,
                    "interval": bybit_iv,
                    "limit": self.config.candle_limit,
                },
            )
            rows = kline_resp.get("result", {}).get("list", []) if isinstance(kline_resp, dict) else []
            tf_ms = TIMEFRAME_MS[tf]
            parsed: List[Candle] = []
            for row in rows:
                # Bybit kline row: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
                open_t = int(row[0])
                close_t = open_t + tf_ms - 1
                parsed.append(
                    Candle(
                        open_time_ms=open_t,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        close_time_ms=close_t,
                        is_closed=(close_t <= now_ms),
                    )
                )
            valid_c, c_issues = self.validate_and_filter_candles(parsed, tf, now_ms, self.config.min_candles_required)
            tf_candles[tf] = valid_c
            issues.extend(c_issues)

        return ctx, tf_candles, issues

    def fetch_symbol_data(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
    ) -> Tuple[Optional[MarketDerivativeContext], Dict[str, List[Candle]], List[str]]:
        tfs = timeframes or self.config.timeframes
        now_ms = self._clock_ms_fn()
        combined_issues: List[str] = []

        # 1. Primary
