"""The small set of Python.NET/LEAN calls used by the adapter.

Imports live here so a memory deployment never requires AlgorithmImports.
Tests inject an API object implementing these operations, not a fake core graph.
"""

from datetime import UTC, datetime, timedelta

from sugarloaf.core.errors import CapabilityError
from sugarloaf.market.contracts import BarsRequirement, LiquidityUniverse


class LeanAPI:
    """Keep platform imports and Python.NET overload selection at one boundary."""

    def __init__(self):
        # Construct this boundary only for native execution. Injected test APIs
        # let shared graph/service code run without importing the LEAN environment.
        import AlgorithmImports as lean

        self.lean = lean

    def setup(self, algorithm, slippage, leverage):
        """Install a native security initializer with proportional slippage and leverage."""
        # Apply models when native securities are initialized, including those
        # admitted later by a dynamic universe, not only the initial tickers.
        def initialize_security(security):
            security.set_slippage_model(self.lean.ConstantSlippageModel(slippage))
            security.set_leverage(leverage)

        algorithm.add_security_initializer(initialize_security)

    def subscribe(self, algorithm, ticker, req):
        """Create a minute equity feed with declared session, normalization, and fill-forward."""
        normalization = getattr(req, "normalization", "raw")
        mode = getattr(self.lean.DataNormalizationMode, normalization.upper())
        # Minute data is the upstream feed even when a node requests daily bars.
        # The adapter's consolidator defines the node-visible interval boundary.
        return algorithm.add_equity(
            ticker,
            self.lean.Resolution.MINUTE,
            fill_forward=req.fill_forward,
            extended_market_hours=req.session == "extended",
            data_normalization_mode=mode,
        ).symbol

    def consolidator(self, req, security):
        """Create native bar/quote consolidation; daily bars use exchange-session boundaries."""
        cls = (
            self.lean.TradeBarConsolidator
            if isinstance(req, BarsRequirement)
            else (self.lean.QuoteBarConsolidator)
        )
        # Daily means the exchange session, not an arbitrary 24-hour bucket.
        # The native calendar handles session opens/closes and shortened days.
        if req.interval == timedelta(days=1):

            def session_calendar(local):
                midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
                hours = security.exchange.hours
                start = hours.get_next_market_open(midnight, req.session == "extended")
                end = hours.get_next_market_close(start, req.session == "extended")
                return self.lean.CalendarInfo(start, end - start)

            return cls(session_calendar)
        return cls(req.interval)

    def history(self, algorithm, symbol, req, cutoff, count):
        """Request typed native warmup history ending at an explicit aware cutoff.

        The finite retrieval span is a warmup attempt, not a readiness guarantee.
        Regular daily bars use daily history; other requests use minute inputs.
        """
        cls = self.lean.TradeBar if isinstance(req, BarsRequirement) else self.lean.QuoteBar
        # Warm minute consolidations with minute data; explicit end prevents lookahead.
        daily = req.interval == timedelta(days=1) and req.session == "regular"
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
            self.lean.Resolution.DAILY if daily else self.lean.Resolution.MINUTE,
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

    def universe(self, algorithm, definition, selector, etf_ticker=None):
        """Create a synchronous minute-resolution native fundamental or ETF universe source."""
        # Copy settings so this source does not mutate the algorithm defaults.
        # Synchronous selection fits the host's serial admission/decision boundary.
        settings = self.lean.UniverseSettings(algorithm.universe_settings)
        settings.asynchronous = False
        settings.minimum_time_in_universe = definition.minimum_residence
        settings.resolution = self.lean.Resolution.MINUTE
        if isinstance(definition, LiquidityUniverse):
            return algorithm.add_universe(self.lean.FundamentalUniverse.usa(selector, settings))
        return algorithm.add_universe(algorithm.universe.etf(etf_ticker, settings, selector))

    def unchanged(self):
        """Return the native sentinel preserving the prior universe selection."""
        return self.lean.Universe.UNCHANGED

    def dispose_universe(self, universe):
        # LEAN performs one final empty selection and preserves held/open-order members.
        """Dispose selection ownership while leaving held/open-order member retention to LEAN."""
        universe.dispose()

    def collect_universe(self, algorithm, universe):
        # Removing the manager entry early would remove these members from ActiveSecurities.
        """Remove a disposed universe manager entry only when selected/member sets are empty."""
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
        """Submit an asynchronous native market order tagged with the intent identity.

        Returns:
            (OrderTicket): Native ticket; submission or cancellation response is not a fill.

        Raises:
            CapabilityError: Intent is not a day market order.
        """
        if intent.order_type != "market" or intent.time_in_force != "day":
            raise CapabilityError("QC gateway supports day market orders only")
        # Preserve the neutral intent ID in the native tag. The host can resolve
        # feedback even if a callback arrives before the ticket-ID map is updated.
        properties = self.lean.OrderProperties()
        properties.time_in_force = self.lean.TimeInForce.DAY
        return algorithm.market_order(
            symbol,
            intent.quantity,
            asynchronous=True,
            tag=intent.intent_id,
            order_properties=properties,
        )

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
        daily = source.interval == timedelta(days=1) and source.session == "regular"
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
            self.lean.Resolution.DAILY if daily else self.lean.Resolution.MINUTE,
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
