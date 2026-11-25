
def _validate_context(
    self,
    ctx: MarketDerivativeContext,
    now_ms: int
) -> None:

    if not ctx.is_real_perpetual:
        raise ExchangeDataError("Non-perpetual market data rejected.")

    if not self._number(ctx.mark_price) or ctx.mark_price <= 0:
        raise ExchangeDataError("Invalid mark price.")

    if ctx.funding_rate is not None:
        if not math.isfinite(ctx.funding_rate):
            raise ExchangeDataError("Invalid funding rate.")

    if ctx.best_bid is None or ctx.best_ask is None:
        raise ExchangeDataError("Order book data unavailable.")

    if (
        ctx.best_bid <= 0
        or ctx.best_ask <= 0
        or ctx.best_ask < ctx.best_bid
    ):
        raise ExchangeDataError("Invalid order book prices.")

    if ctx.spread_pct is None or ctx.spread_pct < 0:
        raise ExchangeDataError("Invalid order book spread.")

    if ctx.quote_volume_24h is None:
        raise ExchangeDataError("24h quote volume unavailable.")

    if ctx.quote_volume_24h < self.config.min_24h_quote_volume_usdt:
        raise ExchangeDataError("24h quote volume below minimum.")

    if ctx.spread_pct > self.config.max_spread_pct:
        raise ExchangeDataError("Spread exceeds configured maximum.")

    if (
        ctx.funding_rate is not None
        and abs(ctx.funding_rate) > self.config.max_abs_funding_rate
    ):
        raise ExchangeDataError("Funding rate exceeds configured limit.")

    # Always validate against the current clock, not a timestamp
    # captured before network requests.
    now_ms = self._clock_ms_fn()

    price_time = getattr(ctx, "price_fetched_at_ms", None)
    funding_time = getattr(ctx, "funding_fetched_at_ms", None)
    book_time = getattr(ctx, "orderbook_fetched_at_ms", None)
    ticker_time = getattr(ctx, "ticker_fetched_at_ms", None)

    timestamps = {
        "price": price_time,
        "funding": funding_time,
        "order book": book_time,
        "ticker": ticker_time,
    }

    for name, timestamp in timestamps.items():
        if timestamp is None:
            raise ExchangeDataError(f"{name} timestamp unavailable.")

        age = now_ms - timestamp

        if age < 0 or age > self.config.max_market_data_age_ms:
            raise ExchangeDataError(f"{name} data is stale.")

    book_age = now_ms - book_time

    if book_age > self.config.max_orderbook_age_ms:
        raise ExchangeDataError("Order book data is stale.")
