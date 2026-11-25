
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .config import ScannerConfig, TIMEFRAME_MS
from .models import Candle, MarketDerivativeContext

logger = logging.getLogger(__name__)


class ExchangeDataError(Exception):
    """Raised when LBank market data is invalid or unavailable."""


class PerpetualDataClient:
    """
    LBank data client.

    Candles:
        LBank Spot API.

    Derivatives context:
        LBank Perpetual API.

    Binance and Bybit are not used.
    Spot candles are explicitly identified as Spot data.
    """

    SPOT_BASE_URL = "https://api.lbkex.com"

    SPOT_KLINE_ENDPOINT = "/v2/kline.do"

    LBANK_ENDPOINTS = {
        "instruments": "/cfd/openApi/v1/pub/instrument",
        "market_data": "/cfd/openApi/v1/pub/marketData",
        "orderbook": "/cfd/openApi/v1/pub/marketOrder",
    }

    SPOT_INTERVALS = {
        "15m": "minute15",
        "1h": "hour1",
        "4h": "hour4",
    }

    def __init__(
        self,
        config: ScannerConfig,
        http_get: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        clock_ms_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self.config = config
        self._http_get = http_get or self._default_http_get
        self._clock_ms_fn = clock_ms_fn or (
            lambda: int(time.time() * 1000)
        )

        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "PerpetualSMCScanner/3.0"}
        )

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            result = float(value)
            if math.isfinite(result):
                return result
        except (TypeError, ValueError, OverflowError):
            pass
        return None

    def _default_http_get(
        self,
        url: str,
        params: Dict[str, Any],
    ) -> Any:
        last_error = None

        retries = max(
            1,
            int(getattr(self.config, "max_retries", 3)),
        )

        timeout = getattr(
            self.config,
            "request_timeout",
            15,
        )

        backoff = getattr(
            self.config,
            "retry_backoff_base",
            1,
        )

        for attempt in range(retries):
            try:
                response = self._session.get(
                    url,
                    params=params,
                    timeout=timeout,
                )

                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ):
                    if attempt + 1 < retries:
                        retry_after = self._number(
                            response.headers.get("Retry-After")
                        )
                        delay = (
                            retry_after
                            if retry_after is not None
                            else backoff * (2 ** attempt)
                        )
                        time.sleep(min(delay, 15))
                        continue

                response.raise_for_status()
                return response.json()

            except (
                requests.RequestException,
                ValueError,
            ) as exc:
                last_error = exc

                if attempt + 1 < retries:
                    time.sleep(
                        min(backoff * (2 ** attempt), 15)
                    )

        raise ExchangeDataError(
            f"LBank HTTP request failed: {last_error}"
        )

    @staticmethod
    def _normalize_symbol(symbol: Any) -> str:
        if symbol is None:
            return ""

        return (
            str(symbol)
            .upper()
            .replace("_", "")
            .replace("-", "")
            .replace("/", "")
            .replace(":", "")
            .strip()
        )

    @staticmethod
    def _lbank_unwrap(data: Any) -> Any:
        if isinstance(data, dict):
            error_code = data.get("error_code")

            if error_code not in (None, 0, "0"):
                raise ExchangeDataError(
                    f"LBank API error: {error_code} - "
                    f"{data.get('msg', '')}"
                )

            if data.get("success") is False:
                raise ExchangeDataError(
                    f"LBank request failed: {data.get('msg', '')}"
                )

            if str(data.get("result", "")).lower() == "false":
                raise ExchangeDataError(
                    f"LBank request failed: {data.get('msg', '')}"
                )

            if "data" in data:
                return data["data"]

        return data

    @staticmethod
    def _find_lbank_symbol(
        rows: Any,
        target_symbol: str,
    ) -> Optional[Dict[str, Any]]:
        if isinstance(rows, dict):
            rows = (
                rows.get("list")
                or rows.get("instruments")
                or rows.get("marketData")
                or []
            )

        if not isinstance(rows, list):
            return None

        target = PerpetualDataClient._normalize_symbol(
            target_symbol
        )

        for row in rows:
            if not isinstance(row, dict):
                continue

            candidate = PerpetualDataClient._normalize_symbol(
                row.get("symbol")
            )

            if candidate == target:
                return row

        return None

    # ---------------------------------------------------------
    # CANDLE VALIDATION
    # ---------------------------------------------------------

    @staticmethod
    def validate_and_filter_candles(
        candles: List[Candle],
        timeframe: str,
        now_ms: int,
        min_required: int,
        max_gap_count: int = 0,
    ) -> Tuple[List[Candle], List[str]]:
        issues: List[str] = []

        if timeframe not in TIMEFRAME_MS:
            return [], [
                f"Unsupported timeframe: {timeframe}"
            ]

        tf_ms = TIMEFRAME_MS[timeframe]
        valid: List[Candle] = []

        for candle in candles:
            try:
                values = [
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                ]

                if not all(
                    math.isfinite(float(value))
                    for value in values
                ):
                    issues.append(
                        f"{timeframe}: non-finite candle value"
                    )
                    continue

                if (
                    not candle.is_closed
                    or candle.close_time_ms > now_ms
                ):
                    continue

                if (
                    candle.open <= 0
                    or candle.high <= 0
                    or candle.low <= 0
                    or candle.close <= 0
                    or candle.volume < 0
                ):
                    issues.append(
                        f"{timeframe}: invalid OHLCV"
                    )
                    continue

                if (
                    candle.high
                    < max(candle.open, candle.close)
                    or candle.low
                    > min(candle.open, candle.close)
                    or candle.high < candle.low
                ):
                    issues.append(
                        f"{timeframe}: inconsistent OHLC"
                    )
                    continue

                if (
                    candle.open_time_ms < 0
                    or candle.close_time_ms
                    < candle.open_time_ms
                ):
                    issues.append(
                        f"{timeframe}: invalid timestamps"
                    )
                    continue

                valid.append(candle)

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                issues.append(
                    f"{timeframe}: malformed candle"
                )

        valid.sort(
            key=lambda item: item.open_time_ms
        )

        deduped: List[Candle] = []
        seen = set()

        for candle in valid:
            if candle.open_time_ms not in seen:
                seen.add(candle.open_time_ms)
                deduped.append(candle)

        gaps = 0

        for index in range(1, len(deduped)):
            delta = (
                deduped[index].open_time_ms
                - deduped[index - 1].open_time_ms
            )

            if delta > tf_ms:
                gaps += 1
            elif delta < tf_ms:
                issues.append(
                    f"{timeframe}: irregular candle timestamps"
                )

        if gaps > max_gap_count:
            issues.append(
                f"{timeframe}: {gaps} candle gaps "
                f"(maximum allowed: {max_gap_count})"
            )

        if len(deduped) < min_required:
            issues.append(
                f"{timeframe}: insufficient closed candles "
                f"({len(deduped)}/{min_required})"
            )

        return deduped, issues

    # ---------------------------------------------------------
    # LBANK SPOT CANDLES
    # ---------------------------------------------------------

    def _parse_spot_candle(
        self,
        row: Any,
        timeframe: str,
    ) -> Optional[Candle]:
        """
        LBank Spot Kline commonly returns array rows:
        [timestamp, volume, close, high, low, open]

        Also accepts dictionary rows when returned by the API.
        """

        try:
            if isinstance(row, dict):
                timestamp = (
                    row.get("timestamp")
                    or row.get("time")
                    or row.get("ts")
                )

                open_price = row.get("open")
                high = row.get("high")
                low = row.get("low")
                close = row.get("close")
                volume = row.get("volume")

            elif isinstance(row, (list, tuple)):
                if len(row) < 6:
                    return None

                timestamp = row[0]
                volume = row[1]
                close = row[2]
                high = row[3]
                low = row[4]
                open_price = row[5]

            else:
                return None

            timestamp = int(float(timestamp))

            # LBank Spot Kline timestamps may be seconds or ms.
            if timestamp < 10_000_000_000:
                timestamp *= 1000

            open_price = float(open_price)
            high = float(high)
            low = float(low)
            close = float(close)
            volume = float(volume)

            tf_ms = TIMEFRAME_MS[timeframe]
            close_time = timestamp + tf_ms - 1

            return Candle(
                open_time_ms=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                close_time_ms=close_time,
                is_closed=(
                    close_time <= self._clock_ms_fn()
                ),
            )

        except (
            TypeError,
            ValueError,
            IndexError,
            OverflowError,
        ):
            return None

    def _fetch_lbank_spot_candles(
        self,
        symbol: str,
        timeframes: List[str],
    ) -> Tuple[Dict[str, List[Candle]], List[str]]:
        result: Dict[str, List[Candle]] = {}
        issues: List[str] = []

        spot_symbol = f"{symbol.lower()}_usdt"

        candle_limit = int(
            getattr(self.config, "candle_limit", 300)
        )

        min_required = max(
            int(
                getattr(
                    self.config,
                    "min_candles_required",
                    220,
                )
            ),
            220,
        )

        max_gaps = int(
            getattr(
                self.config,
                "max_candle_gap_count",
                0,
            )
        )

        for timeframe in timeframes:
            interval = self.SPOT_INTERVALS.get(timeframe)

            if interval is None:
                raise ExchangeDataError(
                    f"Unsupported LBank Spot timeframe: {timeframe}"
                )

            response = self._http_get(
                self.SPOT_BASE_URL + self.SPOT_KLINE_ENDPOINT,
                {
                    "symbol": spot_symbol,
                    "size": candle_limit,
                    "type": interval,
                },
            )

            rows = self._lbank_unwrap(response)

            if isinstance(rows, dict):
                rows = (
                    rows.get("data")
                    or rows.get("list")
                    or rows.get("klines")
                    or []
                )

            if not isinstance(rows, list):
                raise ExchangeDataError(
                    f"LBank Spot invalid Kline response: {timeframe}"
                )

            parsed: List[Candle] = []

            for row in rows:
                candle = self._parse_spot_candle(
                    row,
                    timeframe,
                )

                if candle is None:
                    issues.append(
                        f"LBank Spot malformed candle: {timeframe}"
                    )
                    continue

                parsed.append(candle)

            valid, candle_issues = (
                self.validate_and_filter_candles(
                    parsed,
                    timeframe,
                    self._clock_ms_fn(),
                    min_required,
                    max_gaps,
                )
            )

            result[timeframe] = valid
            issues.extend(candle_issues)

        if issues:
            raise ExchangeDataError(
                "; ".join(issues)
            )

        return result, issues

    # ---------------------------------------------------------
    # LBANK PERPETUAL MARKET CONTEXT
    # ---------------------------------------------------------

    @staticmethod
    def _book_price(level: Any) -> Optional[float]:
        if isinstance(level, dict):
            return PerpetualDataClient._number(
                level.get("price")
                or level.get("p")
            )

        if isinstance(level, (list, tuple)) and level:
            return PerpetualDataClient._number(level[0])

        return None

    def _fetch_lbank_context(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        base_url = getattr(
            self.config,
            "lbank_base_url",
            "https://lbkperp.lbank.com",
        ).rstrip("/")

        product_group = getattr(
            self.config,
            "lbank_product_group",
            "SwapU",
        )

        if not product_group:
            raise ExchangeDataError(
                "LBank product group is not configured."
            )

        instrument_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["instruments"],
            {"productGroup": product_group},
        )

        instruments = self._lbank_unwrap(
            instrument_response
        )

        instrument = self._find_lbank_symbol(
            instruments,
            f"{symbol}USDT",
        )

        if instrument is None:
            raise ExchangeDataError(
                f"LBank Perpetual contract not found: "
                f"{symbol}USDT"
            )

        lbank_symbol = instrument.get("symbol")

        market_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["market_data"],
            {"productGroup": product_group},
        )

        market_rows = self._lbank_unwrap(
            market_response
        )

        market_item = self._find_lbank_symbol(
            market_rows,
            lbank_symbol,
        )

        if market_item is None:
            raise ExchangeDataError(
                f"LBank Perpetual market data unavailable: {symbol}"
            )

        market_time = self._clock_ms_fn()

        book_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["orderbook"],
            {
                "depth": 5,
                "symbol": lbank_symbol,
            },
        )

        book = self._lbank_unwrap(book_response)

        if isinstance(book, dict) and "data" in book:
            book = book["data"]

        if not isinstance(book, dict):
            raise ExchangeDataError(
                "LBank Perpetual invalid order book response."
            )

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not isinstance(bids, list) or not bids:
            raise ExchangeDataError(
                "LBank Perpetual bid side unavailable."
            )

        if not isinstance(asks, list) or not asks:
            raise ExchangeDataError(
                "LBank Perpetual ask side unavailable."
            )

        best_bid = self._book_price(bids[0])
        best_ask = self._book_price(asks[0])

        book_time = self._clock_ms_fn()

        mark_price = self._number(
            market_item.get("markedPrice")
        )

        funding_rate = self._number(
            market_item.get("prePositionFeeRate")
        )

        turnover = self._number(
            market_item.get("turnover")
        )

        if mark_price is None or mark_price <= 0:
            raise ExchangeDataError(
                "LBank Perpetual invalid mark price."
            )

        if (
            best_bid is None
            or best_ask is None
            or best_bid <= 0
            or best_ask <= 0
            or best_ask < best_bid
        ):
            raise ExchangeDataError(
                "LBank Perpetual invalid order book prices."
            )

        mid = (best_bid + best_ask) / 2
        spread = ((best_ask - best_bid) / mid) * 100

        return {
            "mark_price": mark_price,
            "funding_rate": funding_rate,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread,
            "turnover": turnover,
            "market_time": market_time,
            "book_time": book_time,
            "symbol": lbank_symbol,
            "product_group": product_group,
        }

    def _build_context(
        self,
        symbol: str,
        data: Dict[str, Any],
    ) -> MarketDerivativeContext:
        now_ms = self._clock_ms_fn()

        turnover = data.get("turnover")

        # LBank turnover unit must be verified before treating it
        # as USDT quote volume. Do not silently label it as USDT.
        quote_volume = (
            turnover
            if turnover is not None
            else 0.0
        )

        ctx = MarketDerivativeContext(
            symbol=symbol,
            exchange_source="lbank_perpetual",
            mark_price=data["mark_price"],
            index_price=None,
            funding_rate=data["funding_rate"],
            best_bid=data["best_bid"],
            best_ask=data["best_ask"],
            spread_pct=data["spread_pct"],
            quote_volume_24h=quote_volume,
            fetched_at_ms=now_ms,
            is_real_perpetual=True,
            price_fetched_at_ms=data["market_time"],
            funding_fetched_at_ms=data["market_time"],
            orderbook_fetched_at_ms=data["book_time"],
            ticker_fetched_at_ms=data["market_time"],
            candle_exchange_source="lbank_spot",
            context_exchange_source="lbank_perpetual",
        )

        # Retain a warning if the turnover unit has not been verified.
        if turnover is None:
            ctx.warnings.append(
                "LBank Perpetual turnover unavailable; "
                "24h quote volume is not verified."
            )
        else:
            ctx.warnings.append(
                "LBank turnover received; quote-volume unit "
                "must be verified before using it as USDT volume."
            )

        return ctx

    def _validate_context(
        self,
        ctx: MarketDerivativeContext,
    ) -> None:
        if not ctx.is_real_perpetual:
            raise ExchangeDataError(
                "Perpetual market context validation failed."
            )

        if (
            ctx.mark_price is None
            or ctx.mark_price <= 0
        ):
            raise ExchangeDataError(
                "Invalid LBank Perpetual mark price."
            )

        if (
            ctx.best_bid is None
            or ctx.best_ask is None
            or ctx.best_bid <= 0
            or ctx.best_ask < ctx.best_bid
        ):
            raise ExchangeDataError(
                "Invalid LBank Perpetual order book."
            )

        if (
            ctx.funding_rate is not None
            and not math.isfinite(ctx.funding_rate)
        ):
            raise ExchangeDataError(
                "Invalid LBank funding rate."
            )

        if (
            ctx.spread_pct is None
            or ctx.spread_pct < 0
        ):
            raise ExchangeDataError(
                "Invalid LBank spread."
            )

        now_ms = self._clock_ms_fn()

        for label, timestamp in (
            ("price", ctx.price_fetched_at_ms),
            ("funding", ctx.funding_fetched_at_ms),
            ("order book", ctx.orderbook_fetched_at_ms),
            ("ticker", ctx.ticker_fetched_at_ms),
        ):
            if timestamp is None:
                raise ExchangeDataError(
                    f"LBank {label} timestamp unavailable."
                )

            age = now_ms - timestamp

            if age < 0:
                raise ExchangeDataError(
                    f"LBank {label} timestamp is in the future."
                )

            if age > self.config.max_market_data_age_ms:
                raise ExchangeDataError(
                    f"LBank {label} data is stale."
                )

        book_age = (
            now_ms - ctx.orderbook_fetched_at_ms
        )

        if book_age > self.config.max_orderbook_age_ms:
            raise ExchangeDataError(
                "LBank order book data is stale."
            )

        if ctx.spread_pct > self.config.max_spread_pct:
            raise ExchangeDataError(
                "LBank spread exceeds configured maximum."
            )

        if (
            ctx.funding_rate is not None
            and abs(ctx.funding_rate)
            > self.config.max_abs_funding_rate
        ):
            raise ExchangeDataError(
                "LBank funding rate exceeds configured limit."
            )

    # ---------------------------------------------------------
    # MAIN FETCH
    # ---------------------------------------------------------

    def fetch_symbol_data(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
    ) -> Tuple[
        Optional[MarketDerivativeContext],
        Dict[str, List[Candle]],
        List[str],
    ]:
        symbol = symbol.upper().strip()

        if symbol not in self.config.watchlist:
            return None, {}, [
                f"{symbol}: symbol is not in configured watchlist"
            ]

        tfs = list(
            timeframes or self.config.timeframes
        )

        if not tfs:
            return None, {}, [
                "No timeframes configured."
            ]

        if any(
            timeframe not in self.SPOT_INTERVALS
            for timeframe in tfs
        ):
            return None, {}, [
                "Unsupported LBank timeframe requested."
            ]

        try:
            # 1. Fetch LBank Spot candles.
            candles, candle_issues = (
                self._fetch_lbank_spot_candles(
                    symbol,
                    tfs,
                )
            )

            # 2. Fetch LBank Perpetual context.
            derivative_data = self._fetch_lbank_context(
                symbol
            )

            ctx = self._build_context(
                symbol,
                derivative_data,
            )

            self._validate_context(ctx)

            logger.info(
                "%s loaded: candles=LBank Spot, "
                "derivatives=LBank Perpetual",
                symbol,
            )

            return ctx, candles, candle_issues

        except Exception as exc:
            error = (
                f"{symbol}: LBank data unavailable: {exc}"
            )

            logger.error(error)

            return None, {}, [error]

    def close(self) -> None:
        self._session.close()
