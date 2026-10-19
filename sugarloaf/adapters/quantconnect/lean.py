"""The small set of Python.NET/LEAN calls used by the adapter.

Imports live here so a memory deployment never requires AlgorithmImports.
Tests inject an API object implementing these operations, not a fake core graph.
"""

from datetime import UTC, datetime, timedelta

from sugarloaf.core.errors import CapabilityError
from sugarloaf.market.contracts import (
    BarsRequirement,
    FundamentalUniverse,
    LiquidityUniverse,
    QuotesRequirement,
)

# The bar periods LEAN delivers natively for US equities at the resolutions this
# adapter uses. A subscription is described by its period, a neutral timedelta.
MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
# Order types the gateway can translate. LEAN decides when each may be sent and
# how it fills; this adapter only chooses the matching native call.
ORDER_TYPES = ("market", "market_on_open", "market_on_close")
# Neutral time in force -> member of LEAN's TimeInForce.
TIMES_IN_FORCE = {"day": "DAY", "gtc": "GOOD_TIL_CANCELED"}


def native_period(req) -> timedelta:
    """Period of the coarsest native feed that serves one requirement directly.

    A requirement asks for bars or quotes at some interval. LEAN delivers minute,
    hour and daily trade bars, and anything coarser can be consolidated from a
    finer feed but not the reverse. So the coarsest usable feed is the largest of
    those three periods that divides the interval:

    * 1 day -> daily; 1, 2, 3 hours -> hourly; 5 minutes, 90 minutes -> minute.
    * Quotes -> minute: LEAN has no hourly or daily quote bars for equities.
    * An extended session -> minute: native hourly and daily bars cover the
      regular session only.
    """
    if isinstance(req, QuotesRequirement) or req.session != "regular":
        return MINUTE
    for period in (DAY, HOUR):
        if req.interval % period == timedelta(0):
            return period
    return MINUTE


class LeanAPI:
    """Keep platform imports and Python.NET overload selection at one boundary."""

    def __init__(self):
        # Construct this boundary only for native execution. Injected test APIs
        # let shared graph/service code run without importing the LEAN environment.
        import AlgorithmImports as lean

        self.lean = lean

    def setup(self, algorithm, slippage, leverage, borrow_data=None):
        """Install a native security initializer: slippage, leverage, borrow data and a
        starting price."""
        # Apply models when native securities are initialized, including those
        # admitted later by a dynamic universe, not only the initial tickers.
        def initialize_security(security):
            security.set_slippage_model(self.lean.ConstantSlippageModel(slippage))
            security.set_leverage(leverage)
            if borrow_data == "interactive_brokers":
                # The brokerage model does not attach it; without it LEAN reports no
                # borrow data at all.
                security.set_shortable_provider(self.lean.InteractiveBrokersShortableProvider())
                # Charge the provider's borrow fee on short holdings (and credit its rebate);
                # LEAN's default margin interest model charges nothing.
                security.margin_interest_rate_model = self.lean.ShortMarginInterestRateModel()
            # A new security has no price until its first bar arrives, and LEAN
            # refuses an order for it. On a daily feed that is the whole first
            # day (seen natively: members selected in the morning could not be
            # valued at that day's close). Give it its last known price, which is
            # history up to now and not a later value.
            for data in algorithm.get_last_known_prices(security.symbol):
                security.set_market_price(data)

        algorithm.add_security_initializer(initialize_security)

    def resolution(self, period):
        """Translate a native feed period to LEAN's Resolution member."""
        try:
            name = {MINUTE: "MINUTE", HOUR: "HOUR", DAY: "DAILY"}[period]
        except KeyError as error:
            raise CapabilityError("Native feeds are minute, hourly or daily") from error
        return getattr(self.lean.Resolution, name)

    def subscribe(self, algorithm, ticker, req, period=MINUTE):
        """Create an equity feed at ``period`` with declared session, normalization, fill-forward.

        ``period`` is the finest feed any requirement on this security needs (see
        ``native_period``). A node that reads only daily bars gets a daily feed.
        """
        normalization = getattr(req, "normalization", "raw")
        mode = getattr(self.lean.DataNormalizationMode, normalization.upper())
        return algorithm.add_equity(
            ticker,
            self.resolution(period),
            fill_forward=req.fill_forward,
            extended_market_hours=req.session == "extended",
            data_normalization_mode=mode,
        ).symbol

    def feed_period(self, algorithm, symbol, quotes=False):
        """Period of the feed a consolidator registered on ``symbol`` will read.

        LEAN attaches a consolidator to the first subscription of the symbol whose
        data type fits; it does not look at resolution. A security can carry two
        trade-bar subscriptions (for example a daily one from a universe and a
        minute one added later), and then the first decides what the consolidator
        receives. (Observed natively: with daily added first an identity
        consolidator received daily bars, with minute first it received minutes.)
        Returns None when the symbol has no subscription of that type.
        """
        wanted = "QuoteBar" if quotes else "TradeBar"
        service = algorithm.subscription_manager.subscription_data_config_service
        for config in service.get_subscription_data_configs(symbol):
            if str(config.type).split(".")[-1] == wanted:
                name = str(config.resolution).split(".")[-1].upper()
                return {"MINUTE": MINUTE, "HOUR": HOUR, "DAILY": DAY}.get(name)
        return None

    def consolidator(self, req, security, period=MINUTE):
        """Create the native consolidator that turns the ``period`` feed into ``req.interval``.

        When the feed already delivers the requested interval the consolidator is
        LEAN's identity one: it passes each bar through as it arrives. Otherwise
        it is LEAN's trade-bar or quote-bar consolidator. Either way LEAN updates
        it once it is registered on the security; the adapter feeds it by hand
        only to warm a new window from history.
        """
        bars = isinstance(req, BarsRequirement)
        if req.interval == period:
            # A same-period TradeBarConsolidator would hold each bar until the next
            # one arrives. The identity consolidator emits at once.
            return self.lean.IdentityDataConsolidator[
                self.lean.TradeBar if bars else self.lean.QuoteBar
            ]()
        cls = self.lean.TradeBarConsolidator if bars else self.lean.QuoteBarConsolidator
        # Daily means the exchange session, not an arbitrary 24-hour bucket.
        # The native calendar handles session opens/closes and shortened days.
        if req.interval == DAY:

            def session_calendar(local):
                midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
                hours = security.exchange.hours
                start = hours.get_next_market_open(midnight, req.session == "extended")
                end = hours.get_next_market_close(start, req.session == "extended")
                return self.lean.CalendarInfo(start, end - start)

            return cls(session_calendar)
        return cls(req.interval)

    def history(self, algorithm, symbol, req, cutoff, count, period=MINUTE):
        """Request typed native warmup history ending at an explicit aware cutoff.

        The finite retrieval span is a warmup attempt, not a readiness guarantee.
        Regular daily bars always come from daily history, whatever the live feed,
        because warming a long daily window from minutes would read hundreds of
        thousands of bars. Other requests read history at the feed's ``period``.
        """
        cls = self.lean.TradeBar if isinstance(req, BarsRequirement) else self.lean.QuoteBar
        # An explicit end prevents lookahead.
        daily = req.interval == DAY and req.session == "regular"
        # Request a finite cushion for non-trading days. Missing history remains
        # a readiness problem; this estimate does not guarantee enough samples.
        days = max(
            7, count * 3 if daily else int(req.interval.total_seconds() * count / 23400) * 3 + 7
        )
        # History overloads take algorithm-local endpoints. Convert the explicit
        # UTC cutoff rather than asking for a trailing window ending at wall time.
        end = self.lean.Extensions.convert_from_utc(
            cutoff.astimezone(UTC).replace(tzinfo=None), algorithm.time_zone
        )
        return algorithm.history[cls](
            symbol,
            end - timedelta(days=days),
            end,
            self.resolution(DAY if daily else period),
            fill_forward=req.fill_forward,
            extended_market_hours=req.session == "extended",
            data_normalization_mode=getattr(
                self.lean.DataNormalizationMode, getattr(req, "normalization", "raw").upper()
            ),
        )

    def scan(self, consolidator, security, cutoff):
        """Flush a consolidator through the aware cutoff converted to exchange-local time."""
        local = self.lean.Extensions.convert_from_utc(
            cutoff.astimezone(UTC).replace(tzinfo=None), security.exchange.time_zone
        )
        # Scanning can close a due bucket without a new sample at that instant.
        # It still uses the declared cutoff, never a later clock value.
        consolidator.scan(local)

    def to_utc(self, security, value):
        """Convert native exchange-local time to aware UTC; preserve already-aware instants."""
        # Native bar ends are normally exchange-local; aware inputs already name
        # an instant and must not be interpreted in that zone a second time.
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return self.lean.Extensions.convert_to_utc(value, security.exchange.time_zone).replace(
            tzinfo=UTC
        )

    def is_open(self, security, at, extended):
        """Test native exchange hours at an aware instant, optionally including extended hours."""
        local = self.lean.Extensions.convert_from_utc(
            at.astimezone(UTC).replace(tzinfo=None), security.exchange.time_zone
        )
        return security.exchange.hours.is_open(local, extended)

    def session_close(self, security, after, count):
        """Return the aware UTC close of the count-th regular session ending after ``after``.

        LEAN's ``GetNextMarketClose`` searches from its argument non-inclusively:
        asked at exactly 16:00 on a Friday it returns Monday's 16:00, not that
        same Friday close. Calling it ``count`` times, each time from the previous
        answer, therefore steps forward one whole regular session at a time and
        honours holidays and early closes. (Checked against LEAN master
        ``SecurityExchangeHours.cs``; not yet exercised in a native run.)
        """
        local = self.lean.Extensions.convert_from_utc(
            after.astimezone(UTC).replace(tzinfo=None), security.exchange.time_zone
        )
        for _ in range(count):
            # False excludes extended hours: a session horizon ends at a regular close.
            local = security.exchange.hours.get_next_market_close(local, False)
        return self.to_utc(security, local)

    def universe(self, algorithm, definition, selector, etf_ticker=None, feed=None, period=DAY):
        """Create a synchronous native fundamental or ETF universe source.

        Args:
            feed (BarsRequirement | QuotesRequirement | None): The requirement that
                reads this universe's members, or None when no node reads their prices.
            period (timedelta): Feed period for the members LEAN adds (see
                ``native_period``).

        LEAN subscribes each selected member with these settings. They are made
        identical to what ``subscribe`` asks for the same member, so the security
        carries one subscription and not two.
        """
        # Copy settings so this source does not mutate the algorithm defaults.
        # Synchronous selection fits the host's serial admission/decision boundary.
        settings = self.lean.UniverseSettings(algorithm.universe_settings)
        settings.asynchronous = False
        settings.minimum_time_in_universe = definition.minimum_residence
        settings.resolution = self.resolution(period)
        settings.fill_forward = bool(getattr(feed, "fill_forward", False))
        settings.extended_market_hours = getattr(feed, "session", "regular") == "extended"
        settings.data_normalization_mode = getattr(
            self.lean.DataNormalizationMode, getattr(feed, "normalization", "adjusted").upper()
        )
        if isinstance(definition, (LiquidityUniverse, FundamentalUniverse)):
            return algorithm.add_universe(self.lean.FundamentalUniverse.usa(selector, settings))
        return algorithm.add_universe(algorithm.universe.etf(etf_ticker, settings, selector))

    def unchanged(self):
        """Return the native sentinel preserving the prior universe selection."""
        return self.lean.Universe.UNCHANGED

    def dispose_universe(self, universe):
        """Dispose selection ownership while leaving held/open-order member retention to LEAN."""
        # LEAN performs one final empty selection and preserves held/open-order members.
        universe.dispose()

    def collect_universe(self, algorithm, universe):
        """Remove a disposed universe manager entry only when selected/member sets are empty."""
        # Removing the manager entry early would remove these members from ActiveSecurities.
        # Retained members may still back holdings or open orders after selection
        # has stopped. Removal waits for both native collections to become empty.
        if universe.selected or universe.members:
            return False
        algorithm.universe_manager.remove(universe.configuration.symbol)
        return True

    def release_subscription(self, algorithm, symbol):
        """Withdraw manual data ownership without RemoveSecurity's liquidation behavior."""
        security = algorithm.securities[symbol]
        # Resource disposal must not liquidate exposure or discard order tracking.
        # Leave manual ownership in place while either condition still needs it.
        if security.holdings.quantity != 0 or algorithm.transactions.get_open_orders(symbol):
            return False
        key = self.lean.UserDefinedUniverse.create_symbol(symbol.security_type, symbol.id.market)
        if key in algorithm.universe_manager:
            algorithm.universe_manager[key].remove(symbol)
        return True

    def order(self, algorithm, symbol, quantity):
        """Construct a native market-order estimate without submitting it."""
        # This object is for model queries only; no transaction is submitted.
        request = algorithm.order_factory.market_order(symbol, quantity)
        return self.lean.Order.create_order(request)

    def buying_power(self, algorithm, security, quantity):
        """Compare required initial margin with native direction-specific available buying power."""
        order = self.order(algorithm, security.symbol, quantity)
        # Use LEAN's configured buying-power model and order direction instead of
        # approximating margin with cash or a fixed leverage rule in the graph.
        margin = security.buying_power_model.get_initial_margin_required_for_order(
            self.lean.InitialMarginRequiredForOrderParameters(
                algorithm.portfolio.cash_book, security, order
            )
        ).value
        return abs(margin) <= algorithm.portfolio.get_buying_power(security.symbol, order.direction)

    def fee(self, algorithm, security, quantity):
        """Estimate the native order fee in account currency using cash-book FX.

        Raises:
            CapabilityError: Fee-currency conversion is missing or nonpositive.
        """
        order = self.order(algorithm, security.symbol, quantity)
        fee = security.fee_model.get_order_fee(self.lean.OrderFeeParameters(security, order)).value
        if fee.currency not in algorithm.portfolio.cash_book:
            raise CapabilityError("No account FX conversion for platform fee currency")
        # A fee may be denominated in another currency. Convert through the same
        # native cash book used by the account before returning a neutral estimate.
        rate = algorithm.portfolio.cash_book[fee.currency].conversion_rate
        if rate <= 0:
            raise CapabilityError("Platform fee currency FX conversion is not ready")
        return float(fee.amount * rate)

    def submit(self, algorithm, symbol, intent):
        """Submit an asynchronous native order tagged with the intent identity.

        Returns:
            (OrderTicket): Native ticket; submission or cancellation response is not a fill.

        Raises:
            CapabilityError: The order type or time in force has no native call here.

        The adapter chooses the native call and nothing else. When an order may be
        sent and how it fills are LEAN's decisions, set by the backtest's brokerage
        and fill models. (Observed natively on LEAN 2.5.0.18178: a market order
        sent while the exchange is closed, or on a daily feed, becomes a
        market-on-open order; a market-on-close order sent inside the last
        quarter-hour, and a market-on-open order sent during the session on a
        minute feed, come back invalid. A day order sent at the close on a daily
        feed expires unfilled: its fill price arrives with the next daily bar, at
        the close that ends its day.)
        """
        if intent.order_type not in ORDER_TYPES or intent.time_in_force not in TIMES_IN_FORCE:
            raise CapabilityError(
                "QC gateway supports day or gtc market, open and close orders only"
            )
        # Preserve the neutral intent ID in the native tag. The host can resolve
        # feedback even if a callback arrives before the ticket-ID map is updated.
        properties = self.lean.OrderProperties()
        properties.time_in_force = getattr(
            self.lean.TimeInForce, TIMES_IN_FORCE[intent.time_in_force]
        )
        send = {
            "market": algorithm.market_order,
            "market_on_open": algorithm.market_on_open_order,
            "market_on_close": algorithm.market_on_close_order,
        }[intent.order_type]
        return send(
            symbol,
            intent.quantity,
            asynchronous=True,
            tag=intent.intent_id,
            order_properties=properties,
        )

    def refusal(self, ticket):
        """Return why LEAN refused to create an order, or None if it was accepted.

        A refused order has no platform order behind it and produces no order
        event, so the ticket is the only place the outcome is visible.
        """
        if str(ticket.status).split(".")[-1].lower() != "invalid":
            return None
        response = ticket.get_most_recent_order_response()
        if response is None:
            return "order refused"
        return f"{response.error_code}: {response.error_message}"

    def indicator(self, requirement):
        """Construct manually updated native SMA, EMA, RSI, or Donchian resources.

        Returns:
            (tuple[object, Mapping[str, object], str]): Native indicator, named outputs,
                and selected input field.

        Raises:
            CapabilityError: Kind, period, field, settings, or output names are unsupported.

        The adapter owns one update path; no automatic indicator registration is used.
        """
        # Work on a copy of declared parameters. Every accepted setting is consumed
        # explicitly; leftovers fail instead of silently choosing native defaults.
        params = dict(requirement.parameters)
        field = params.pop("field", "close")
        if field not in {"open", "high", "low", "close", "volume"}:
            raise CapabilityError("Unsupported indicator input field")
        period = params.pop("period", None)
        if isinstance(period, bool) or not isinstance(period, int) or period < 1:
            raise CapabilityError("Indicator period must be a positive integer")
        # Native constructors own formulas and initialization. These are manually
        # updated resources, not QCAlgorithm helpers with automatic registration.
        if requirement.kind == "sma":
            indicator = self.lean.SimpleMovingAverage(period)
            outputs = {"value": indicator}
        elif requirement.kind == "ema":
            smoothing = params.pop("smoothing_factor", None)
            indicator = (
                self.lean.ExponentialMovingAverage(period)
                if smoothing is None
                else self.lean.ExponentialMovingAverage(period, smoothing)
            )
            outputs = {"value": indicator}
        elif requirement.kind == "rsi":
            average = params.pop("moving_average_type", "wilders")
            try:
                kind = getattr(self.lean.MovingAverageType, average.upper())
            except AttributeError as error:
                raise CapabilityError("Unsupported native RSI moving average type") from error
            indicator = self.lean.RelativeStrengthIndex(period, kind)
            outputs = {"value": indicator}
        elif requirement.kind == "donchian":
            if field != "close":
                raise CapabilityError("Donchian consumes complete bars, not a selected field")
            indicator = self.lean.DonchianChannel(period)
            outputs = {
                "value": indicator,
                "upper": indicator.upper_band,
                "lower": indicator.lower_band,
            }
        else:
            raise CapabilityError("Unsupported native indicator kind")
        if params or set(requirement.outputs) - set(outputs):
            raise CapabilityError("Unsupported native indicator parameters or outputs")
        # Retain the requested number of native outputs. Resource readiness also
        # needs enough post-warmup samples, which the services layer checks.
        for output in outputs.values():
            output.window.size = requirement.window
        return indicator, outputs, field

    def update_indicator(self, indicator, kind, field, bar):
        """Feed one native bar to Donchian or its selected scalar field to another indicator."""
        # Donchian needs high/low bars; scalar indicators receive one chosen field
        # with its native bar-end timestamp. There is only one update call per path.
        if kind == "donchian":
            indicator.update(bar)
        else:
            indicator.update(bar.end_time, getattr(bar, field))

    def indicator_version(self):
        """Return the native LEAN version string, or unknown when unavailable."""
        return str(
            getattr(self.lean.Globals, "version", getattr(self.lean.Globals, "Version", "unknown"))
        )

    def schedule_due(self, algorithm, symbols, schedule, after, through):
        """Resolve wall/session times through LEAN's timezone and exchange databases."""
        from NodaTime import DateTimeZoneProviders

        # Python.NET can expose either naming convention for the native database.
        # Keep that compatibility detail out of shared Schedule contracts.
        database = getattr(DateTimeZoneProviders, "tzdb", None)
        if database is None:
            database = DateTimeZoneProviders.Tzdb
        # Session anchors follow the instrument exchange; wall-time schedules use
        # their declared zone. Convert occurrences back to UTC before comparison.
        zone = (
            algorithm.securities[symbols[schedule.calendar]].exchange.time_zone
            if schedule.anchor
            else database[schedule.timezone]
        )
        local = self.lean.Extensions.convert_from_utc(
            through.astimezone(UTC).replace(tzinfo=None), zone
        )
        lower = after or self.lean.Extensions.convert_to_utc(
            local.replace(hour=0, minute=0, second=0, microsecond=0), zone
        ).replace(tzinfo=UTC)
        start = self.lean.Extensions.convert_from_utc(
            lower.astimezone(UTC).replace(tzinfo=None), zone
        )
        # Offsets can move an occurrence across midnight. Search neighboring days
        # as well as the unshifted interval before applying the crossing test.
        day = (start - abs(schedule.offset)).date()
        last = (local + abs(schedule.offset)).date()
        while day <= last:
            midnight = datetime.combine(day, datetime.min.time())
            if schedule.calendar:
                security = algorithm.securities[symbols[schedule.calendar]]
                hours = security.exchange.hours
                opening = hours.get_next_market_open(midnight, False)
                if opening.date() != day:
                    day += timedelta(days=1)
                    continue
                if schedule.anchor:
                    point = (
                        opening
                        if schedule.anchor == "open"
                        else hours.get_next_market_close(opening, False)
                    )
                    due = self.to_utc(security, point) + schedule.offset
                else:
                    point = datetime.combine(day, schedule.at)
                    due = (
                        self.lean.Extensions.convert_to_utc(point, zone).replace(tzinfo=UTC)
                        + schedule.offset
                    )
            else:
                point = datetime.combine(day, schedule.at)
                due = (
                    self.lean.Extensions.convert_to_utc(point, zone).replace(tzinfo=UTC)
                    + schedule.offset
                )
            # A crossed occurrence is open on the left and closed on the right,
            # preventing the same boundary from firing again on the next batch.
            if lower < due <= through:
                return True
            day += timedelta(days=1)
        return False

    def history_range(self, algorithm, symbol, source, start, end):
        """Typed, lazy native history; research admits these samples incrementally."""
        cls = self.lean.TradeBar
        # ``source`` describes a replay feed, so its interval is a native period.
        local_start = self.lean.Extensions.convert_from_utc(
            start.astimezone(UTC).replace(tzinfo=None), algorithm.time_zone
        )
        local_end = self.lean.Extensions.convert_from_utc(
            end.astimezone(UTC).replace(tzinfo=None), algorithm.time_zone
        )
        # Return native history lazily. QuantBookServices stages and admits each
        # observation later; retrieval itself must not advance graph-visible state.
        return algorithm.history[cls](
            symbol,
            local_start,
            local_end,
            self.resolution(native_period(source)),
            fill_forward=source.fill_forward,
            extended_market_hours=source.session == "extended",
            data_normalization_mode=getattr(
                self.lean.DataNormalizationMode, source.normalization.upper()
            ),
        )

    def corporate_actions(self, algorithm, symbol, start, end):
        """Sparse native split/dividend facts staged by causal research replay."""
        local_start = self.lean.Extensions.convert_from_utc(
            start.astimezone(UTC).replace(tzinfo=None), algorithm.time_zone
        )
        local_end = self.lean.Extensions.convert_from_utc(
            end.astimezone(UTC).replace(tzinfo=None), algorithm.time_zone
        )
        # Retrieve sparse effective facts for replay ordering. Their availability
        # is controlled by staged event boundaries, not by this retrieval loop.
        for kind in (self.lean.Split, self.lean.Dividend):
            for item in algorithm.history[kind](
                symbol, local_start, local_end, self.lean.Resolution.DAILY
            ):
                if kind == self.lean.Split and "warning" in str(item.type).lower():
                    continue
                yield item
