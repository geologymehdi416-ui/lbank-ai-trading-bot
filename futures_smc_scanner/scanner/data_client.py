import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .config import ScannerConfig, TIMEFRAME_MS
from .models import Candle, MarketDerivativeContext

logger = logging.getLogger(__name__)


class ExchangeDataError(Exception):
    """Raised when real perpetual market data is invalid or unavailable."""


class PerpetualDataClient:
    """
    Perpetual market data client.

    Binance USD-M and Bybit USDT Linear provide OHLCV candles.
    LBank provides supplemental perpetual contract market context only.
    LBank Spot candles are never used.
    """

    BYBIT_INTERVAL_MAP = {"15m": "15", "1h": "60", "4h": "240"}

    LBANK_ENDPOINTS = {
        "instruments": "/cfd/openApi/v1/pub/instrument",
        "market_data": "/cfd/openApi/v1/pub/marketData",
        "orderbook": "/cfd/openApi/v1/pub/marketOrder",
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
            {"User-Agent": "PerpetualSMCScanner/2.1"}
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
        self, url: str, params: Dict[str, Any]
    ) -> Any:
        lowered = url.lower()

        forbidden_paths = (
            "/api/v3/",
            "api.binance.com/api",
            "/spot/",
            "/v2/kline.do",
        )
        if any(path in lowered for path in forbidden_paths):
            raise ExchangeDataError(
                "Spot endpoint access is forbidden."
            )

        last_error = None

        for attempt in range(self.config.max_retries):
            try:
                response = self._session.get(
                    url,
                    params=params,
                    timeout=self.config.request_timeout,
                )

                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ):
                    if attempt + 1 < self.config.max_retries:
                        retry_after = self._number(
                            response.headers.get("Retry-After")
                        )
                        delay = (
                            retry_after
                            if retry_after is not None
                            else self.config.retry_backoff_base
                            * (2 ** attempt)
                        )
                        time.sleep(min(delay, 15))
                        continue

                response.raise_for_status()
                return response.json()

            except requests.RequestException as exc:
                last_error = exc
            except ValueError as exc:
                last_error = exc

            if attempt + 1 < self.config.max_retries:
                time.sleep(
                    min(
                        self.config.retry_backoff_base
                        * (2 ** attempt),
                        15,
                    )
                )

        raise ExchangeDataError(
            f"HTTP request failed after retries: {last_error}"
        )

    @staticmethod
    def _validate_exchange_response(
        data: Any, exchange: str
    ) -> None:
        if not isinstance(data, dict):
            raise ExchangeDataError(
                f"{exchange}: invalid response format"
            )

        if exchange == "bybit" and data.get("retCode") != 0:
            raise ExchangeDataError(
                f"Bybit API error: "
                f"{data.get('retMsg', 'unknown')}"
            )

        if exchange == "binance" and "code" in data:
            raise ExchangeDataError(
                f"Binance API error: "
                f"{data.get('msg', data.get('code'))}"
            )

    # ---------------- COMMON HELPERS ----------------

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
        """
        Supports direct arrays/objects and common LBank envelopes.
        Raises an error when the API explicitly reports failure.
        """
        if isinstance(data, dict):
            error_code = data.get("error_code")

            if error_code not in (None, 0, "0"):
                raise ExchangeDataError(
                    f"LBank API error: "
                    f"{error_code} - {data.get('msg', '')}"
                )

            if data.get("success") is False:
                raise ExchangeDataError(
                    f"LBank request failed: {data.get('msg', '')}"
                )

            if data.get("result") is False:
                raise ExchangeDataError(
                    f"LBank request failed: {data.get('msg', '')}"
                )

            if "data" in data:
                return data["data"]

        return data

    @staticmethod
    def _find_lbank_symbol(
        rows: Any, target_symbol: str
    ) -> Optional[Dict[str, Any]]:
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

    # ---------------- CANDLE VALIDATION ----------------

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
            return [], [f"Unsupported timeframe: {timeframe}"]

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
                        f"{timeframe}: invalid candle timestamps"
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

            if delta != tf_ms:
                if delta > tf_ms:
                    gaps += 1
                else:
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

    # ---------------- DERIVATIVE CONTEXT VALIDATION ----------------

    def _validate_context(
        self,
        ctx: MarketDerivativeContext,
        now_ms: Optional[int] = None,
    ) -> None:
        if not ctx.is_real_perpetual:
            raise ExchangeDataError(
                "Non-perpetual market data rejected."
            )

        if (
            not self._number(ctx.mark_price)
            or ctx.mark_price <= 0
        ):
            raise ExchangeDataError("Invalid mark price.")

        if (
            ctx.funding_rate is not None
            and not math.isfinite(ctx.funding_rate)
        ):
            raise ExchangeDataError("Invalid funding rate.")

        if (
            ctx.best_bid is None
            or ctx.best_ask is None
        ):
            raise ExchangeDataError(
                "Order book data unavailable."
            )

        if (
            ctx.best_bid <= 0
            or ctx.best_ask <= 0
            or ctx.best_ask < ctx.best_bid
        ):
            raise ExchangeDataError(
                "Invalid order book prices."
            )

        if (
            ctx.spread_pct is None
            or ctx.spread_pct < 0
        ):
            raise ExchangeDataError(
                "Invalid order book spread."
            )

        if ctx.quote_volume_24h is None:
            raise ExchangeDataError(
                "24h quote volume unavailable."
            )

        if (
            ctx.quote_volume_24h
            < self.config.min_24h_quote_volume_usdt
        ):
            raise ExchangeDataError(
                "24h quote volume below minimum."
            )

        if ctx.spread_pct > self.config.max_spread_pct:
            raise ExchangeDataError(
                "Spread exceeds configured maximum."
            )

        if (
            ctx.funding_rate is not None
            and abs(ctx.funding_rate)
            > self.config.max_abs_funding_rate
        ):
            raise ExchangeDataError(
                "Funding rate exceeds configured limit."
            )

        current_ms = self._clock_ms_fn()

        timestamp_fields = (
            (
                "price",
                ctx.price_fetched_at_ms,
            ),
            (
                "funding",
                ctx.funding_fetched_at_ms,
            ),
            (
                "order book",
                ctx.orderbook_fetched_at_ms,
            ),
            (
                "ticker",
                ctx.ticker_fetched_at_ms,
            ),
        )

        for label, timestamp in timestamp_fields:
            if timestamp is None:
                raise ExchangeDataError(
                    f"{label} timestamp unavailable."
                )

            age = current_ms - timestamp

            if (
                age < 0
                or age > self.config.max_market_data_age_ms
            ):
                raise ExchangeDataError(
                    f"{label} data is stale."
                )

        book_timestamp = ctx.orderbook_fetched_at_ms
        book_age = current_ms - book_timestamp

        if book_age > self.config.max_orderbook_age_ms:
            raise ExchangeDataError(
                "Order book data is stale."
            )

    def _validate_timeframe_data(
        self,
        candles: Dict[str, List[Candle]],
        issues: List[str],
        timeframes: List[str],
    ) -> None:
        if issues:
            raise ExchangeDataError("; ".join(issues))

        for timeframe in timeframes:
            if timeframe not in candles:
                raise ExchangeDataError(
                    f"Missing timeframe: {timeframe}"
                )

            required = max(
                self.config.min_candles_required,
                220,
            )

            if len(candles[timeframe]) < required:
                raise ExchangeDataError(
                    f"Insufficient candles for {timeframe}: "
                    f"{len(candles[timeframe])}/{required}"
                )

    # ---------------- LBANK PERPETUAL SUPPLEMENT ----------------

    def _fetch_lbank_context(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        """
        Fetches supplemental LBank perpetual contract data.

        This method does not fetch or use Spot candles.
        It does not replace the primary exchange's market context.
        """

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

        # Confirm that the contract exists in the selected product group.
        instrument_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["instruments"],
            {"productGroup": product_group},
        )

        instruments = self._lbank_unwrap(
            instrument_response
        )

        if isinstance(instruments, dict):
            instruments = instruments.get(
                "list",
                instruments.get("instruments", []),
            )

        instrument = self._find_lbank_symbol(
            instruments,
            f"{symbol}USDT",
        )

        if instrument is None:
            raise ExchangeDataError(
                f"LBank perpetual contract not found: "
                f"{symbol}USDT ({product_group})"
            )

        # Get LBank contract market data.
        market_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["market_data"],
            {"productGroup": product_group},
        )

        market_rows = self._lbank_unwrap(
            market_response
        )

        if isinstance(market_rows, dict):
            market_rows = market_rows.get(
                "list",
                market_rows.get("marketData", []),
            )

        market_item = self._find_lbank_symbol(
            market_rows,
            instrument.get("symbol"),
        )

        if market_item is None:
            raise ExchangeDataError(
                f"LBank market data unavailable for {symbol}."
            )

        market_time = self._clock_ms_fn()

        # Use the exact contract symbol returned by LBank.
        lbank_symbol = instrument.get("symbol")

        # Get the contract order book.
        book_response = self._http_get(
            base_url + self.LBANK_ENDPOINTS["orderbook"],
            {
                "depth": 5,
                "symbol": lbank_symbol,
            },
        )

        book = self._lbank_unwrap(book_response)

        if not isinstance(book, dict):
            raise ExchangeDataError(
                "LBank invalid order book response."
            )

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not isinstance(bids, list) or not bids:
            raise ExchangeDataError(
                "LBank bid side unavailable."
            )

        if not isinstance(asks, list) or not asks:
            raise ExchangeDataError(
                "LBank ask side unavailable."
            )

        best_bid = self._number(
            bids[0].get("price")
            if isinstance(bids[0], dict)
            else None
        )

        best_ask = self._number(
            asks[0].get("price")
            if isinstance(asks[0], dict)
            else None
        )

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
                "LBank invalid marked price."
            )

        if (
            best_bid is None
            or best_ask is None
            or best_bid <= 0
            or best_ask <= 0
            or best_ask < best_bid
        ):
            raise ExchangeDataError(
                "LBank invalid order book prices."
            )

        return {
            "mark_price": mark_price,
            "funding_rate": funding_rate,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "turnover": turnover,
            "market_fetched_at_ms": market_time,
            "book_fetched_at_ms": book_time,
            "symbol": lbank_symbol,
            "product_group": product_group,
        }

    def _attach_lbank_context(
        self,
        ctx: MarketDerivativeContext,
        symbol: str,
    ) -> None:
        """
        Adds LBank data without replacing Binance/Bybit values.

        LBank failures are non-fatal because it is a supplemental source.
        """

        try:
            data = self._fetch_lbank_context(symbol)

            ctx.lbank_mark_price = data["mark_price"]
            ctx.lbank_funding_rate = data["funding_rate"]
            ctx.lbank_best_bid = data["best_bid"]
            ctx.lbank_best_ask = data["best_ask"]
            ctx.lbank_quote_volume_24h = data["turnover"]
            ctx.lbank_fetched_at_ms = max(
                data["market_fetched_at_ms"],
                data["book_fetched_at_ms"],
            )

            ctx.context_exchange_source = (
                f"{ctx.exchange_source}+lbank_contract_context"
            )

            ctx.warnings.append(
                "LBank contract context attached as supplemental data; "
                "primary candles and trading context remain unchanged."
            )

            logger.info(
                "LBank supplemental contract data loaded for %s",
                symbol,
            )

        except Exception as exc:
            warning = (
                f"LBank supplemental data unavailable for "
                f"{symbol}: {exc}"
            )

            ctx.warnings.append(warning)

            logger.warning(warning)

    # ---------------- BINANCE USD-M FUTURES ----------------

    def _fetch_binance_symbol(
        self,
        symbol: str,
        timeframes: List[str],
        now_ms: int,
    ) -> Tuple[
        MarketDerivativeContext,
        Dict[str, List[Candle]],
        List[str],
    ]:
        pair = f"{symbol}USDT"
        issues: List[str] = []

        premium = self._http_get(
            f"{self.config.binance_base_url}/fapi/v1/premiumIndex",
            {"symbol": pair},
        )

        self._validate_exchange_response(
            premium,
            "binance",
        )

        price_time = self._clock_ms_fn()

        book = self._http_get(
            f"{self.config.binance_base_url}/fapi/v1/ticker/bookTicker",
            {"symbol": pair},
        )

        self._validate_exchange_response(
            book,
            "binance",
        )

        book_time = self._clock_ms_fn()

        ticker = self._http_get(
            f"{self.config.binance_base_url}/fapi/v1/ticker/24hr",
            {"symbol": pair},
        )

        self._validate_exchange_response(
            ticker,
            "binance",
        )

        ticker_time = self._clock_ms_fn()

        mark = self._number(premium.get("markPrice"))
        index = self._number(premium.get("indexPrice"))
        funding = self._number(
            premium.get("lastFundingRate")
        )

        bid = self._number(book.get("bidPrice"))
        ask = self._number(book.get("askPrice"))
        quote_volume = self._number(
            ticker.get("quoteVolume")
        )

        if mark is None or mark <= 0:
            raise ExchangeDataError(
                "Binance invalid mark price."
            )

        if (
            bid is None
            or ask is None
            or bid <= 0
            or ask < bid
        ):
            raise ExchangeDataError(
                "Binance invalid order book."
            )

        if quote_volume is None or quote_volume < 0:
            raise ExchangeDataError(
                "Binance invalid quote volume."
            )

        mid = (bid + ask) / 2
        spread = ((ask - bid) / mid) * 100

        ctx = MarketDerivativeContext(
            symbol=symbol,
            exchange_source="binance_usds_m_perpetual",
            mark_price=mark,
            index_price=index,
            funding_rate=funding,
            best_bid=bid,
            best_ask=ask,
            spread_pct=spread,
            quote_volume_24h=quote_volume,
            fetched_at_ms=ticker_time,
            is_real_perpetual=True,
            price_fetched_at_ms=price_time,
            funding_fetched_at_ms=price_time,
            orderbook_fetched_at_ms=book_time,
            ticker_fetched_at_ms=ticker_time,
            candle_exchange_source="binance_usds_m_perpetual",
            context_exchange_source="binance_usds_m_perpetual",
        )

        self._validate_context(ctx)

        result: Dict[str, List[Candle]] = {}

        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_MS:
                raise ExchangeDataError(
                    f"Unsupported timeframe: {timeframe}"
                )

            rows = self._http_get(
                f"{self.config.binance_base_url}/fapi/v1/klines",
                {
                    "symbol": pair,
                    "interval": timeframe,
                    "limit": self.config.candle_limit,
                },
            )

            if not isinstance(rows, list):
                raise ExchangeDataError(
                    f"Binance invalid kline response: {timeframe}"
                )

            parsed: List[Candle] = []

            for row in rows:
                try:
                    if len(row) < 7:
                        continue

                    close_time = int(row[6])

                    parsed.append(
                        Candle(
                            open_time_ms=int(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            close_time_ms=close_time,
                            is_closed=(
                                close_time <= self._clock_ms_fn()
                            ),
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                    IndexError,
                ):
                    issues.append(
                        f"Binance malformed candle: {timeframe}"
                    )

            filter_now = self._clock_ms_fn()

            valid, candle_issues = (
                self.validate_and_filter_candles(
                    parsed,
                    timeframe,
                    filter_now,
                    max(
                        self.config.min_candles_required,
                        220,
                    ),
                    self.config.max_candle_gap_count,
                )
            )

            result[timeframe] = valid
            issues.extend(candle_issues)

        self._validate_timeframe_data(
            result,
            issues,
            timeframes,
        )

        return ctx, result, issues

    # ---------------- BYBIT USDT LINEAR FUTURES ----------------

    def _fetch_bybit_symbol(
        self,
        symbol: str,
        timeframes: List[str],
        now_ms: int,
    ) -> Tuple[
        MarketDerivativeContext,
        Dict[str, List[Candle]],
        List[str],
    ]:
        pair = f"{symbol}USDT"

        ticker_response = self._http_get(
            f"{self.config.bybit_base_url}/v5/market/tickers",
            {
                "category": "linear",
                "symbol": pair,
            },
        )

        self._validate_exchange_response(
            ticker_response,
            "bybit",
        )

        items = (
            ticker_response.get("result", {})
            .get("list", [])
        )

        if not items:
            raise ExchangeDataError(
                f"Bybit linear contract unavailable: {pair}"
            )

        item = items[0]

        mark = self._number(item.get("markPrice"))
        index = self._number(item.get("indexPrice"))
        funding = self._number(item.get("fundingRate"))
        bid = self._number(item.get("bid1Price"))
        ask = self._number(item.get("ask1Price"))
        quote_volume = self._number(
            item.get("turnover24h")
        )

        ticker_time = self._clock_ms_fn()

        if mark is None or mark <= 0:
            raise ExchangeDataError(
                "Bybit invalid mark price."
            )

        if (
            bid is None
            or ask is None
            or bid <= 0
            or ask < bid
        ):
            raise ExchangeDataError(
                "Bybit invalid order book."
            )

        if quote_volume is None or quote_volume < 0:
            raise ExchangeDataError(
                "Bybit invalid quote volume."
            )

        mid = (bid + ask) / 2
        spread = ((ask - bid) / mid) * 100

        ctx = MarketDerivativeContext(
            symbol=symbol,
            exchange_source="bybit_usdt_linear_perpetual",
            mark_price=mark,
            index_price=index,
            funding_rate=funding,
            best_bid=bid,
            best_ask=ask,
            spread_pct=spread,
            quote_volume_24h=quote_volume,
            fetched_at_ms=ticker_time,
            is_real_perpetual=True,
            price_fetched_at_ms=ticker_time,
            funding_fetched_at_ms=ticker_time,
            orderbook_fetched_at_ms=ticker_time,
            ticker_fetched_at_ms=ticker_time,
            candle_exchange_source="bybit_usdt_linear_perpetual",
            context_exchange_source="bybit_usdt_linear_perpetual",
        )

        self._validate_context(ctx)

        result: Dict[str, List[Candle]] = {}
        issues: List[str] = []

        for timeframe in timeframes:
            interval = self.BYBIT_INTERVAL_MAP.get(
                timeframe
            )

            if interval is None:
                raise ExchangeDataError(
                    f"Unsupported Bybit timeframe: {timeframe}"
                )

            response = self._http_get(
                f"{self.config.bybit_base_url}/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": pair,
                    "interval": interval,
                    "limit": self.config.candle_limit,
                },
            )

            self._validate_exchange_response(
                response,
                "bybit",
            )

            rows = (
                response.get("result", {})
                .get("list", [])
            )

            if not isinstance(rows, list):
                raise ExchangeDataError(
                    f"Bybit invalid kline response: {timeframe}"
                )

            parsed: List[Candle] = []
            tf_ms = TIMEFRAME_MS[timeframe]

            for row in rows:
                try:
                    if len(row) < 6:
                        continue

                    open_time = int(row[0])
                    close_time = open_time + tf_ms - 1

                    parsed.append(
                        Candle(
                            open_time_ms=open_time,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            close_time_ms=close_time,
                            is_closed=(
                                close_time <= self._clock_ms_fn()
                            ),
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                    IndexError,
                ):
                    issues.append(
                        f"Bybit malformed candle: {timeframe}"
                    )

            filter_now = self._clock_ms_fn()

            valid, candle_issues = (
                self.validate_and_filter_candles(
                    parsed,
                    timeframe,
                    filter_now,
                    max(
                        self.config.min_candles_required,
                        220,
                    ),
                    self.config.max_candle_gap_count,
                )
            )

            result[timeframe] = valid
            issues.extend(candle_issues)

        self._validate_timeframe_data(
            result,
            issues,
            timeframes,
        )

        return ctx, result, issues

    # ---------------- PUBLIC DATA FETCH ----------------

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
            timeframe not in TIMEFRAME_MS
            for timeframe in tfs
        ):
            return None, {}, [
                "Unsupported timeframe requested."
            ]

        ctx = None
        candles = None

        # Primary source: Binance USD-M Futures.
        try:
            ctx, candles, _issues = (
                self._fetch_binance_symbol(
                    symbol,
                    tfs,
                    self._clock_ms_fn(),
                )
            )

        except Exception as exc:
            logger.warning(
                "Binance Futures failed for %s: %s",
                symbol,
                exc,
            )

        # Fallback source: Bybit USDT Linear Perpetual.
        if ctx is None or candles is None:
            try:
                ctx, candles, _issues = (
                    self._fetch_bybit_symbol(
                        symbol,
                        tfs,
                        self._clock_ms_fn(),
                    )
                )

            except Exception as exc:
                logger.error(
                    "Bybit Linear Futures failed for %s: %s",
                    symbol,
                    exc,
                )

                return None, {}, [
                    f"{symbol}: Binance and Bybit Futures "
                    f"data unavailable. Bybit error: {exc}"
                ]

        # Supplemental source: LBank perpetual contracts.
        # Failure here must not invalidate valid Binance/Bybit data.
        self._attach_lbank_context(
            ctx,
            symbol,
        )

        return ctx, candles, []

    def close(self) -> None:
        self._session.close()