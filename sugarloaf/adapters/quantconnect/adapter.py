"""The QuantConnect adapter: each Sugarloaf declaration mapped to one LEAN call.

Setup (called by the runner before the clock starts):

* ``Bars``/``Quotes``: ``add_security``; a ``TradeBarConsolidator`` for an interval;
  a ``RollingWindow`` per instrument.
* ``Universe``: ``add_universe(FundamentalUniverse.usa(...))`` or ``universe.etf(...)``,
  with ``universe_settings.schedule`` for the cadence.
* ``Custom("earnings")``: ``add_data(EODHDUpcomingEarnings, ...)``; items from
  ``slice.get(EODHDUpcomingEarnings)``.
* ``Indicator``: ``algorithm.sma/ema/rsi/dch(symbol, period, resolution or interval)``
  with automatic warm-up.
* ``Schedule``: ``schedule.on(date_rules..., time_rules..., fn)``.
* ``WarmUp``: ``set_warm_up``.

Reads: ``history[TradeBar](...)``, ``securities[symbol].exchange.hours``,
``portfolio``, ``transactions``. Orders: ``market_order`` and the on-open and
on-close variants with the target id as the tag. Results: ``object_store.save``.

LEAN's callbacks (``on_data``, ``on_securities_changed``, ``on_order_event``,
scheduled functions, consolidator handlers) each forward to the runner. Native
timestamps are exchange-local; they are converted with the exchange's zone once
per security. LEAN objects never reach a node.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sugarloaf.adapters.protocol import Arrival, IndicatorReading
from sugarloaf.core.data import (
    Bar,
    BorrowTerms,
    Delisting,
    Dividend,
    EarningsListing,
    FundamentalRecord,
    QuoteBar,
    Split,
    SymbolChange,
)
from sugarloaf.core.errors import CapabilityError, ContractError, NotReady
from sugarloaf.core.inputs import (
    RESOLUTION_PERIODS,
    Bars,
    Custom,
    ETFConstituentsUniverse,
    FundamentalUniverse,
    HistoryRequest,
    Indicator,
    LiquidityUniverse,
    Quotes,
    Schedule,
    Universe,
    WarmUp,
)
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.universes import SelectionRow, select
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import (
    ExecutionPolicy,
    OpenOrder,
    OrderRecord,
    OrderUpdate,
    Portfolio,
    desired_quantity,
    order_quantity,
)

# Neutral asset class -> LEAN SecurityType member, for the classes this adapter subscribes.
SECURITY_TYPES = {"equity": "EQUITY", "index": "INDEX"}
RESOLUTION_NAMES = {"minute": "MINUTE", "hour": "HOUR", "daily": "DAILY"}
EARNINGS_TIMINGS = {
    "BEFORE_MARKET": "before_open",
    "BEFOREMARKET": "before_open",
    "AFTER_MARKET": "after_close",
    "AFTERMARKET": "after_close",
}
ORDER_STATUSES = {
    "new": "new",
    "submitted": "submitted",
    "partiallyfilled": "partial",
    "filled": "filled",
    "canceled": "canceled",
    "cancelpending": "pending_cancel",
    "invalid": "invalid",
    "updatesubmitted": "submitted",
}


def _name(value) -> str:
    """The member name of a LEAN enum value, as Python.NET prints it."""
    return str(value).split(".")[-1].lower().replace("_", "")


class Ticker:
    """A ticker and asset class bound to an instrument in a deployment's mapping."""

    def __init__(self, ticker: str, asset_class: str = "equity"):
        self.ticker, self.asset_class = ticker, asset_class


class NativeWindow(Sequence):
    """A LEAN ``RollingWindow`` read as neutral bars, newest first, converted on access."""

    def __init__(self, window, convert):
        self._window, self._convert = window, convert

    def __len__(self) -> int:
        return int(self._window.count)

    def __getitem__(self, index):
        count = len(self)
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(count)))
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError(index)
        return self._convert(self._window[index])

    def column(self, name: str, count: int | None = None) -> tuple[float, ...]:
        """A bar field over the newest ``count`` bars, oldest first, without building bars."""
        n = len(self) if count is None else min(count, len(self))
        return tuple(float(getattr(self._window[i], name)) for i in range(n - 1, -1, -1))

    def oldest_first(self, count: int | None = None) -> tuple:
        n = len(self) if count is None else min(count, len(self))
        return tuple(self[i] for i in range(n - 1, -1, -1))


class NativeCalendar:
    """An instrument's exchange hours through LEAN's ``SecurityExchangeHours``."""

    def __init__(self, security, zone: ZoneInfo):
        self.hours, self.zone = security.exchange.hours, zone

    def _local(self, at: datetime):
        return at.astimezone(self.zone).replace(tzinfo=None)

    def _utc(self, local) -> datetime:
        return local.replace(tzinfo=self.zone).astimezone(UTC)

    def next_close(self, after: datetime, count: int = 1) -> datetime:
        # GetNextMarketClose searches non-inclusively, so calling it ``count`` times from
        # the previous answer steps one regular session at a time, holidays included.
        local = self._local(after)
        for _ in range(count):
            local = self.hours.get_next_market_close(local, False)
        return self._utc(local)

    def next_open(self, after: datetime, count: int = 1) -> datetime:
        local = self._local(after)
        for _ in range(count):
            local = self.hours.get_next_market_open(local, False)
        return self._utc(local)

    def is_open(self, at: datetime, extended: bool = False) -> bool:
        return bool(self.hours.is_open(self._local(at), extended))

    def session(self, at: datetime, extended: bool = False) -> tuple[datetime, datetime]:
        opened = self.hours.get_previous_market_open(self._local(at), extended)
        closed = self.hours.get_next_market_close(opened, extended)
        return self._utc(opened), self._utc(closed)


class QuantConnectAdapter:
    """Map Sugarloaf declarations and reads onto a ``QCAlgorithm``.

    Args:
        algorithm: The native algorithm.
        tickers: Instrument -> ticker (or ``Ticker`` with an asset class) for the
            instruments nodes name explicitly. Universe members bind by security id.
        slippage: Proportional slippage applied to every security's fill model.
        leverage: Security leverage.
        borrow_data: None, or ``"interactive_brokers"`` for IB borrow terms and fees.
        lean: The ``AlgorithmImports`` namespace; injected by tests.
    """

    def __init__(
        self,
        algorithm,
        tickers: Mapping[InstrumentId, str | Ticker] | None = None,
        *,
        slippage: float = 0.0005,
        leverage: float = 1.0,
        borrow_data: str | None = None,
        lean=None,
    ):
        if lean is None:
            import AlgorithmImports as lean
        self.lean, self.algorithm = lean, algorithm
        self.runner = None
        self.tickers = {
            k: v if isinstance(v, Ticker) else Ticker(v) for k, v in (tickers or {}).items()
        }
        self.symbols: dict[InstrumentId, object] = {}
        self.instruments: dict[str, InstrumentId] = {}
        self._zones: dict[str, ZoneInfo] = {}
        self.feeds: dict[tuple, Bars | Quotes] = {}
        self.windows: dict[tuple, dict[InstrumentId, object]] = {}
        self.consolidators: dict[tuple, dict[InstrumentId, object]] = {}
        self._completed: dict[tuple, dict[InstrumentId, object]] = {}
        self.universes: dict[str, tuple[Universe, tuple, object]] = {}
        self.memberships: dict[str, tuple[InstrumentId, ...]] = {}
        self._selected: dict[str, tuple[InstrumentId, ...]] = {}
        self.customs: dict[str, Custom] = {}
        self.indicators: dict[tuple[Indicator, InstrumentId], object] = {}
        self._indicator_declarations: list[Indicator] = []
        self.schedules: dict[str, Schedule] = {}
        self.tags: dict[str, str] = {}
        self._last_daily_custom = None
        self._earnings_symbol = None
        self._pending_history = None
        algorithm.settings.automatic_indicator_warm_up = True
        algorithm.settings.daily_precise_end_time = True

        def initialize_security(security):
            security.set_slippage_model(lean.ConstantSlippageModel(slippage))
            security.set_leverage(leverage)
            if borrow_data == "interactive_brokers":
                security.set_shortable_provider(lean.InteractiveBrokersShortableProvider())
                security.margin_interest_rate_model = lean.ShortMarginInterestRateModel()

        algorithm.add_security_initializer(initialize_security)
        algorithm.settings.seed_initial_prices = True

    # ------------------------------------------------------------- identities

    def bind(self, symbol) -> InstrumentId:
        """The instrument for a native symbol, by its permanent security id."""
        key = str(symbol.id)
        instrument = self.instruments.get(key)
        if instrument is None:
            instrument = InstrumentId(key)
            self.instruments[key] = instrument
        self.symbols.setdefault(instrument, symbol)
        return instrument

    def _symbol(self, instrument: InstrumentId):
        try:
            return self.symbols[instrument]
        except KeyError:
            raise CapabilityError(f"{instrument.value} is not subscribed") from None

    def _security(self, instrument: InstrumentId):
        return self.algorithm.securities[self._symbol(instrument)]

    def _zone(self, symbol) -> ZoneInfo:
        key = str(symbol.id)
        zone = self._zones.get(key)
        if zone is None:
            zone = ZoneInfo(str(self.algorithm.securities[symbol].exchange.time_zone.id))
            self._zones[key] = zone
        return zone

    def _utc(self, symbol, local) -> datetime:
        return local.replace(tzinfo=self._zone(symbol)).astimezone(UTC)

    def _resolution(self, resolution: str):
        return getattr(self.lean.Resolution, RESOLUTION_NAMES[resolution])

    def _normalization(self, normalization: str):
        return getattr(self.lean.DataNormalizationMode, normalization.upper())

    # ---------------------------------------------------------------- setup

    def subscribe(self, declaration, instruments) -> None:
        key = declaration.key
        current = self.feeds.get(key)
        if current is None or current.window < declaration.window:
            self.feeds[key] = declaration
        for instrument in instruments:
            if instrument not in self.symbols:
                self._add_security(instrument, declaration)
            self._track(key, instrument)

    def _add_security(self, instrument: InstrumentId, declaration) -> None:
        ticker = self.tickers.get(instrument)
        if ticker is None:
            raise CapabilityError(f"No ticker binding for {instrument.value}")
        try:
            kind = getattr(self.lean.SecurityType, SECURITY_TYPES[ticker.asset_class])
        except KeyError as error:
            raise CapabilityError(f"QC adapter does not subscribe {ticker.asset_class}") from error
        symbol = self.lean.Symbol.create(ticker.ticker, kind, self.lean.Market.USA)
        security = self.algorithm.add_security(
            symbol,
            self._resolution(declaration.resolution),
            fill_forward=getattr(declaration, "fill_forward", False),
            extended_market_hours=declaration.extended_hours,
            data_normalization_mode=self._normalization(
                getattr(declaration, "normalization", "raw")
            ),
        )
        self.symbols[instrument] = security.symbol
        self.instruments[str(security.symbol.id)] = instrument

    def _track(self, key: tuple, instrument: InstrumentId) -> None:
        """A rolling window, and a consolidator when the declaration has an interval."""
        windows = self.windows.setdefault(key, {})
        if instrument in windows:
            return
        feed = self.feeds[key]
        symbol = self._symbol(instrument)
        quotes = isinstance(feed, Quotes)
        bar_type = self.lean.QuoteBar if quotes else self.lean.TradeBar
        window = self.lean.RollingWindow[bar_type](feed.window)
        windows[instrument] = window
        if feed.interval is None:
            return
        consolidator = self._consolidator(feed, symbol, quotes)

        def completed(sender, bar, key=key, instrument=instrument, window=window):
            window.add(bar)
            self._completed.setdefault(key, {})[instrument] = bar

        consolidator.data_consolidated += completed
        self.algorithm.subscription_manager.add_consolidator(symbol, consolidator)
        self.consolidators.setdefault(key, {})[instrument] = consolidator

    def _consolidator(self, feed, symbol, quotes: bool):
        cls = self.lean.QuoteBarConsolidator if quotes else self.lean.TradeBarConsolidator
        if feed.interval != timedelta(days=1):
            return cls(feed.interval)
        # Daily means the exchange session, not a 24-hour bucket: the calendar
        # function gives LEAN each session's open and length.
        security = self.algorithm.securities[symbol]

        def session_calendar(local):
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            hours = security.exchange.hours
            start = hours.get_next_market_open(midnight, feed.extended_hours)
            end = hours.get_next_market_close(start, feed.extended_hours)
            return self.lean.CalendarInfo(start, end - start)

        return cls(session_calendar)

    def add_universe(self, definition: Universe, feeds) -> None:
        feeds = tuple(feeds)
        for feed in feeds:
            current = self.feeds.get(feed.key)
            if current is None or current.window < feed.window:
                self.feeds[feed.key] = feed
        bars = next((f for f in feeds if isinstance(f, Bars)), None)
        finest = min((RESOLUTION_PERIODS[f.resolution] for f in feeds), default=timedelta(days=1))
        resolution = next(r for r, p in RESOLUTION_PERIODS.items() if p == finest)
        settings = self.lean.UniverseSettings(self.algorithm.universe_settings)
        settings.asynchronous = False
        settings.minimum_time_in_universe = definition.minimum_residence
        settings.resolution = self._resolution(resolution)
        settings.fill_forward = bool(getattr(bars, "fill_forward", False))
        settings.extended_market_hours = any(f.extended_hours for f in feeds)
        settings.data_normalization_mode = self._normalization(
            getattr(bars, "normalization", "adjusted")
        )
        rule = {
            "weekly": "week_start",
            "monthly": "month_start",
            "yearly": "year_start",
        }.get(definition.cadence)
        if rule is not None:
            settings.schedule.on(getattr(self.algorithm.date_rules, rule)())
        name = definition.name

        def selector(data):
            at = self.time
            rows = []
            for item in data:
                instrument = self.bind(item.symbol)
                if isinstance(definition, FundamentalUniverse):
                    rows.append(_fundamental_record(instrument, item, at))
                elif isinstance(definition, LiquidityUniverse):
                    rows.append(
                        SelectionRow(
                            instrument,
                            float(item.price),
                            float(item.volume),
                            float(item.dollar_volume),
                            has_fundamentals=getattr(item, "has_fundamental_data", None),
                        )
                    )
                else:
                    weight = getattr(item, "weight", None)
                    rows.append(
                        SelectionRow(instrument, weight=None if weight is None else float(weight))
                    )
            try:
                members = select(definition, rows, at)
            except NotReady:
                # Missing inputs leave the membership as it was.
                return self.lean.Universe.UNCHANGED
            self._selected[name] = members
            return [self.symbols[i] for i in members]

        if isinstance(definition, ETFConstituentsUniverse):
            ticker = self.tickers.get(definition.etf)
            if ticker is None:
                raise CapabilityError("ETF universe requires a ticker binding for the ETF")
            universe = self.algorithm.add_universe(
                self.algorithm.universe.etf(ticker.ticker, settings, selector)
            )
        else:
            universe = self.algorithm.add_universe(
                self.lean.FundamentalUniverse.usa(selector, settings)
            )
        self.universes[name] = (definition, feeds, universe)
        self.memberships[name] = ()

    def add_custom(self, declaration: Custom) -> None:
        self.customs[declaration.kind] = declaration
        if declaration.kind == "earnings" and self._earnings_symbol is None:
            kind = getattr(self.lean, "EODHDUpcomingEarnings", None)
            if kind is None:
                from QuantConnect.DataSource import EODHDUpcomingEarnings as kind
            self._earnings_type = kind
            self._earnings_symbol = self.algorithm.add_data(kind, "earnings").symbol

    def add_indicator(self, declaration: Indicator, instruments) -> None:
        if declaration not in self._indicator_declarations:
            self._indicator_declarations.append(declaration)
        for instrument in instruments:
            if instrument not in self.symbols:
                self._add_security(instrument, declaration.bars)
            self._create_indicator(declaration, instrument)

    def _create_indicator(self, declaration: Indicator, instrument: InstrumentId) -> None:
        if (declaration, instrument) in self.indicators:
            return
        symbol = self._symbol(instrument)
        algorithm = self.algorithm
        cadence = declaration.interval or self._resolution(declaration.resolution)
        params = dict(declaration.parameters)
        field = None if declaration.field == "close" else getattr(
            self.lean.Field, declaration.field.capitalize()
        )
        if declaration.kind == "sma":
            native = algorithm.sma(symbol, declaration.period, cadence, field)
        elif declaration.kind == "ema":
            smoothing = params.get("smoothing_factor")
            native = (
                algorithm.ema(symbol, declaration.period, cadence, field)
                if smoothing is None
                else algorithm.ema(symbol, declaration.period, float(smoothing), cadence, field)
            )
        elif declaration.kind == "rsi":
            kind = str(params.get("moving_average_type", "wilders")).upper()
            average = getattr(self.lean.MovingAverageType, kind)
            native = algorithm.rsi(symbol, declaration.period, average, cadence, field)
        elif declaration.kind == "donchian":
            native = algorithm.dch(symbol, declaration.period, cadence)
        else:
            raise CapabilityError(f"Unsupported native indicator kind {declaration.kind}")
        native.window.size = declaration.window
        self.indicators[(declaration, instrument)] = native

    def schedule(self, declaration: Schedule) -> None:
        algorithm = self.algorithm
        self.schedules[declaration.name] = declaration
        symbol = self._symbol(declaration.calendar) if declaration.calendar else None
        arguments = () if symbol is None else (symbol,)
        date_rule = getattr(algorithm.date_rules, declaration.days)(*arguments)
        if declaration.after_open is not None:
            time_rule = algorithm.time_rules.after_market_open(symbol, declaration.after_open)
        elif declaration.before_close is not None:
            time_rule = algorithm.time_rules.before_market_close(symbol, declaration.before_close)
        else:
            from NodaTime import DateTimeZoneProviders

            database = getattr(DateTimeZoneProviders, "tzdb", None) or DateTimeZoneProviders.Tzdb
            time_rule = algorithm.time_rules.at(
                declaration.at.hour, declaration.at.minute, database[declaration.timezone]
            )
        name = declaration.name
        algorithm.schedule.on(date_rule, time_rule, lambda: self.runner.on_scheduled(name))

    def warm_up(self, declaration: WarmUp) -> None:
        if declaration.span is not None:
            self.algorithm.set_warm_up(declaration.span)
        else:
            self.algorithm.set_warm_up(declaration.count, self._resolution(declaration.resolution))

    # -------------------------------------------------------------- mapping

    @property
    def time(self) -> datetime:
        value = self.algorithm.utc_time
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def window(self, declaration, instrument: InstrumentId):
        window = self.windows.get(declaration.key, {}).get(instrument)
        if window is None:
            raise CapabilityError(f"{instrument.value} is not subscribed for this feed")
        symbol = self._symbol(instrument)
        convert = (
            (lambda bar: self._quote(instrument, symbol, bar))
            if isinstance(declaration, Quotes)
            else (lambda bar: self._bar(instrument, symbol, bar))
        )
        return NativeWindow(window, convert)

    def indicator(self, declaration: Indicator, instrument: InstrumentId) -> IndicatorReading:
        native = self.indicators.get((declaration, instrument))
        if native is None:
            raise CapabilityError(f"No {declaration.name} indicator for {instrument.value}")
        symbol = self._symbol(instrument)
        current = native.current
        values = tuple(
            (self._utc(symbol, point.end_time), float(point.value))
            for point in (native.window[i] for i in range(int(native.window.count)))
        )
        outputs = {}
        if declaration.kind == "donchian":
            outputs = {
                "upper": float(native.upper_band.current.value),
                "lower": float(native.lower_band.current.value),
            }
        return IndicatorReading(
            bool(native.is_ready),
            None if current is None else float(current.value),
            None if current is None else self._utc(symbol, current.end_time),
            values,
            outputs,
        )

    def members(self, universe: str) -> tuple[InstrumentId, ...]:
        if universe not in self.universes:
            raise CapabilityError(f"No universe named {universe}")
        return self.members_of(universe)

    def members_of(self, universe: str) -> tuple[InstrumentId, ...]:
        return self.memberships.get(universe, ())

    def history(self, request: HistoryRequest) -> Mapping[InstrumentId, tuple[Bar, ...]]:
        if request.interval is not None:
            raise CapabilityError("History is read at the feed resolution; consolidate it yourself")
        symbols = [self._symbol(i) for i in request.instruments]
        resolution = self._resolution(request.resolution)
        mode = self._normalization(request.normalization)
        if request.count is not None:
            slices = self.algorithm.history[self.lean.TradeBar](
                symbols, request.count, resolution, fill_forward=False,
                extended_market_hours=request.extended_hours, data_normalization_mode=mode,
            )
        else:
            local_end = self.algorithm.time
            slices = self.algorithm.history[self.lean.TradeBar](
                symbols, local_end - request.span, local_end, resolution, fill_forward=False,
                extended_market_hours=request.extended_hours, data_normalization_mode=mode,
            )
        result = {i: [] for i in request.instruments}
        for data in slices:
            for symbol, bar in data.items():
                instrument = self.instruments.get(str(symbol.id))
                if instrument in result:
                    result[instrument].append(self._bar(instrument, symbol, bar))
        return {i: tuple(bars) for i, bars in result.items()}

    def calendar(self, instrument: InstrumentId) -> NativeCalendar:
        symbol = self._symbol(instrument)
        return NativeCalendar(self.algorithm.securities[symbol], self._zone(symbol))

    def portfolio(self) -> Portfolio:
        algorithm = self.algorithm
        holdings, prices = {}, {}
        for instrument, symbol in self.symbols.items():
            security = algorithm.securities[symbol]
            quantity = float(security.holdings.quantity)
            if quantity:
                holdings[instrument] = quantity
            price = float(security.price)
            if price > 0:
                prices[instrument] = price
        for kvp in algorithm.portfolio:
            holding = kvp.Value if hasattr(kvp, "Value") else kvp.value
            if holding.invested:
                instrument = self.bind(holding.symbol)
                holdings[instrument] = float(holding.quantity)
                if float(holding.price) > 0:
                    prices[instrument] = float(holding.price)
        orders = []
        for order in algorithm.transactions.get_open_orders():
            ticket = algorithm.transactions.get_order_ticket(order.id)
            orders.append(
                OpenOrder(
                    str(order.id), self.bind(order.symbol), float(order.quantity),
                    float(ticket.quantity_filled), str(order.tag or ""),
                )
            )
        return Portfolio(
            self.time,
            float(algorithm.portfolio.total_portfolio_value),
            float(algorithm.portfolio.cash),
            holdings,
            prices,
            tuple(orders),
            str(algorithm.account_currency),
        )

    def execute(self, target: PositionView, policy: ExecutionPolicy) -> tuple[OrderRecord, ...]:
        algorithm = self.algorithm
        account = self.portfolio()
        target_id = target.metadata.view_id
        records = []
        send = {
            "market": algorithm.market_order,
            "market_on_open": algorithm.market_on_open_order,
            "market_on_close": algorithm.market_on_close_order,
        }[policy.order_type]
        properties = self.lean.OrderProperties()
        properties.time_in_force = getattr(
            self.lean.TimeInForce, {"day": "DAY", "gtc": "GOOD_TIL_CANCELED"}[policy.time_in_force]
        )
        for instrument in sorted(target.positions, key=lambda i: i.value):
            amount = target.positions[instrument]
            symbol = self.symbols.get(instrument)
            price = account.prices.get(instrument)
            if symbol is None or price is None or price <= 0:
                records.append(OrderRecord(target_id, instrument, 0.0, None, "skipped", "no mark"))
                continue
            if policy.cancel_on_new_target:
                for order in account.open_orders:
                    if order.instrument == instrument and order.tag != target_id:
                        algorithm.transactions.cancel_order(int(order.order_id), "superseded")
                pending = sum(
                    o.remaining for o in account.open_orders
                    if o.instrument == instrument and o.tag == target_id
                )
            else:
                pending = account.pending(instrument)
            lot = float(algorithm.securities[symbol].symbol_properties.lot_size)
            whole, exact = desired_quantity(amount, target.units, price, account.nav, lot)
            quantity = order_quantity(
                whole, exact, account.holdings.get(instrument, 0.0), pending, price, account.nav,
                target.units, policy,
            )
            if quantity == 0:
                continue
            ticket = send(symbol, quantity, asynchronous=True, tag=target_id,
                          order_properties=properties)
            if _name(ticket.status) == "invalid":
                response = ticket.get_most_recent_order_response()
                reason = "refused" if response is None else (
                    f"{response.error_code}: {response.error_message}"
                )
                records.append(
                    OrderRecord(target_id, instrument, quantity, None, "rejected", reason)
                )
                continue
            self.tags[str(ticket.order_id)] = target_id
            records.append(OrderRecord(target_id, instrument, quantity, str(ticket.order_id)))
        return tuple(records)

    def save(self, key: str, text: str) -> None:
        self.algorithm.object_store.save(key, text)

    # ------------------------------------------------------------ callbacks

    def on_data(self, data) -> None:
        """Build one arrival from a LEAN slice and this slice's consolidated bars."""
        time = self.time
        bars: dict = {}
        quotes: dict = {}
        for key, feed in self.feeds.items():
            windows = self.windows.get(key)
            if not windows:
                continue
            if feed.interval is not None:
                completed = self._completed.pop(key, None)
                if completed:
                    target = quotes if isinstance(feed, Quotes) else bars
                    convert = self._quote if isinstance(feed, Quotes) else self._bar
                    target[key] = {
                        instrument: convert(instrument, self.symbols[instrument], bar)
                        for instrument, bar in completed.items()
                    }
                continue
            source = data.quote_bars if isinstance(feed, Quotes) else data.bars
            convert = self._quote if isinstance(feed, Quotes) else self._bar
            delivered = {}
            for symbol, bar in source.items():
                instrument = self.instruments.get(str(symbol.id))
                window = windows.get(instrument)
                if window is None:
                    continue
                window.add(bar)
                delivered[instrument] = convert(instrument, symbol, bar)
            if delivered:
                (quotes if isinstance(feed, Quotes) else bars)[key] = delivered
        custom = self._custom(data, time)
        lifecycle = self._lifecycle(data, time)
        arrival = Arrival(
            time, bars, quotes, custom, *lifecycle,
            warming_up=bool(self.algorithm.is_warming_up),
        )
        self.runner.on_data(arrival)

    def _custom(self, data, time: datetime) -> dict:
        custom: dict = {}
        if "earnings" in self.customs:
            listings = []
            items = data.get(self._earnings_type)
            for symbol, item in (items.items() if items is not None else ()):
                instrument = self.instruments.get(str(item.symbol.id))
                if instrument is None:
                    continue
                report = item.report_date
                listings.append(
                    EarningsListing(
                        instrument,
                        time,
                        report.date() if hasattr(report, "date") else report,
                        EARNINGS_TIMINGS.get(
                            str(item.report_time).upper().replace(" ", ""), "unknown"
                        ),
                    )
                )
            if listings:
                custom["earnings"] = tuple(listings)
        wanted = {k for k in ("fundamentals", "borrow") if k in self.customs}
        day = time.date()
        if wanted and self._last_daily_custom != day:
            # Fundamental and borrow records are daily facts: read once a day.
            self._last_daily_custom = day
            instruments = self._custom_instruments()
            if "fundamentals" in wanted:
                records = []
                for instrument in instruments:
                    security = self.algorithm.securities[self.symbols[instrument]]
                    record = security.fundamentals
                    if record is None or not getattr(record, "has_fundamental_data", True):
                        continue
                    records.append(_fundamental_record(instrument, record, time))
                if records:
                    custom["fundamentals"] = tuple(records)
            if "borrow" in wanted:
                records = []
                for instrument in instruments:
                    security = self.algorithm.securities[self.symbols[instrument]]
                    provider = security.shortable_provider
                    if provider is None or type(provider).__name__ == "NullShortableProvider":
                        continue
                    local = self.algorithm.time
                    records.append(
                        BorrowTerms(
                            instrument, time,
                            _number(provider.fee_rate(security.symbol, local)),
                            _number(provider.shortable_quantity(security.symbol, local)),
                        )
                    )
                if records:
                    custom["borrow"] = tuple(records)
        return custom

    def _custom_instruments(self) -> tuple[InstrumentId, ...]:
        result = []
        for declaration in self.customs.values():
            if declaration.kind == "earnings":
                continue
            selection = declaration.selection
            if selection.universe is not None:
                result.extend(self.members_of(selection.universe))
            elif selection.instruments:
                result.extend(selection.instruments)
            else:
                result.extend(self.symbols)
        return tuple(dict.fromkeys(i for i in result if i in self.symbols))

    def _lifecycle(self, data, time: datetime):
        dividends, splits, delistings, changes = [], [], [], []
        for symbol, item in getattr(data, "dividends", {}).items():
            dividends.append(
                Dividend(self.bind(symbol), time, float(item.distribution),
                         _number(getattr(item, "reference_price", None)))
            )
        for symbol, item in getattr(data, "splits", {}).items():
            if "warning" in str(getattr(item, "type", "")).lower():
                continue
            splits.append(
                Split(self.bind(symbol), time, float(item.split_factor),
                      _number(getattr(item, "reference_price", None)))
            )
        for symbol, item in getattr(data, "delistings", {}).items():
            delistings.append(Delisting(self.bind(symbol), time))
        for symbol, item in getattr(data, "symbol_changed_events", {}).items():
            changes.append(SymbolChange(self.bind(symbol), time, item.old_symbol, item.new_symbol))
        return tuple(dividends), tuple(splits), tuple(delistings), tuple(changes)

    def on_securities_changed(self, changes) -> None:
        """Bind new securities, create members' windows and indicators, notify the runner."""
        added = {self.bind(s.symbol) for s in changes.added_securities}
        removed = {self.bind(s.symbol) for s in changes.removed_securities}
        for name, (definition, feeds, _) in self.universes.items():
            selected = set(self._selected.get(name, ()))
            before = set(self.memberships.get(name, ()))
            joined = tuple(
                i for i in self._selected.get(name, ()) if i in added and i not in before
            )
            left = tuple(
                i for i in self.memberships.get(name, ()) if i in removed or i not in selected
            )
            if not joined and not left:
                continue
            for feed in feeds:
                for instrument in joined:
                    self._track(feed.key, instrument)
                for instrument in left:
                    consolidator = self.consolidators.get(feed.key, {}).pop(instrument, None)
                    if consolidator is not None:
                        self.algorithm.subscription_manager.remove_consolidator(
                            self.symbols[instrument], consolidator
                        )
                    self.windows.get(feed.key, {}).pop(instrument, None)
            for declaration in self._indicator_declarations:
                if declaration.selection.universe != name:
                    continue
                for instrument in joined:
                    self._create_indicator(declaration, instrument)
                for instrument in left:
                    native = self.indicators.pop((declaration, instrument), None)
                    if native is not None:
                        self.algorithm.deregister_indicator(native)
            if joined:
                self._warm_members(feeds, joined)
            self.memberships[name] = tuple(
                i for i in self._selected.get(name, ()) if i in before - set(left) or i in joined
            )
            self.runner.on_securities_changed(name, joined, left)

    def _warm_members(self, feeds, instruments) -> None:
        """Fill new members' windows from history, through their consolidators."""
        for feed in feeds:
            if not isinstance(feed, Bars) or feed.window <= 1 and feed.interval is None:
                continue
            symbols = [self.symbols[i] for i in instruments]
            count = feed.window * (
                1
                if feed.interval is None
                else int(feed.interval / RESOLUTION_PERIODS[feed.resolution])
            )
            slices = self.algorithm.history[self.lean.TradeBar](
                symbols, count, self._resolution(feed.resolution), fill_forward=feed.fill_forward,
                extended_market_hours=feed.extended_hours,
                data_normalization_mode=self._normalization(feed.normalization),
            )
            windows = self.windows[feed.key]
            consolidators = self.consolidators.get(feed.key, {})
            for data in slices:
                for symbol, bar in data.items():
                    instrument = self.instruments.get(str(symbol.id))
                    if instrument not in windows:
                        continue
                    if instrument in consolidators:
                        consolidators[instrument].update(bar)
                    else:
                        windows[instrument].add(bar)
            # Bars consolidated from history are warm-up, not this slice's news.
            self._completed.pop(feed.key, None)

    def on_order_event(self, event) -> None:
        ticket = self.algorithm.transactions.get_order_ticket(event.order_id)
        status = _name(event.status)
        if status not in ORDER_STATUSES:
            raise ContractError(f"Unsupported QC order status: {status}")
        order = self.algorithm.transactions.get_order_by_id(event.order_id)
        tag = self.tags.get(str(event.order_id), str(getattr(order, "tag", "") or ""))
        self.runner.on_order_event(
            OrderUpdate(
                str(event.order_id), self.bind(event.symbol), ORDER_STATUSES[status],
                float(ticket.quantity_filled), self.time, tag, str(event.message or ""),
            )
        )

    # ------------------------------------------------------------ converters

    def _bar(self, instrument: InstrumentId, symbol, bar) -> Bar:
        return Bar(
            instrument,
            self._utc(symbol, bar.time),
            self._utc(symbol, bar.end_time),
            float(bar.open), float(bar.high), float(bar.low), float(bar.close),
            float(bar.volume), bool(getattr(bar, "is_fill_forward", False)),
        )

    def _quote(self, instrument: InstrumentId, symbol, bar) -> QuoteBar | None:
        if bar.bid is None or bar.ask is None:
            return None
        return QuoteBar(
            instrument,
            self._utc(symbol, bar.time),
            self._utc(symbol, bar.end_time),
            float(bar.bid.close), float(bar.ask.close),
            float(bar.last_bid_size), float(bar.last_ask_size),
        )


def _number(value):
    if value is None:
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _fundamental_record(instrument, item, at) -> FundamentalRecord:
    reference = getattr(item, "company_reference", None)
    cik = getattr(reference, "cik", None) if reference is not None else None
    cik = (str(cik).strip().lstrip("0") or None) if cik is not None else None
    classification = getattr(item, "asset_classification", None)
    sector = getattr(classification, "morningstar_sector_code", None) if classification else None
    try:
        sector = int(sector) or None if sector is not None else None
    except (TypeError, ValueError):
        sector = None
    return FundamentalRecord(
        instrument,
        at,
        cik=cik,
        market_cap=_number(getattr(item, "market_cap", None)) or None,
        price=_number(getattr(item, "price", None)) or None,
        dollar_volume=_number(getattr(item, "dollar_volume", None)),
        sector=sector,
    )
