"""A small stand-in for LEAN's Python surface, enough to exercise the QuantConnect adapter.

It mirrors the calls the adapter makes: securities with exchange hours in New York
time, trade-bar consolidators with ``data_consolidated`` events, rolling windows,
indicator helpers that LEAN would update from data, history by count, a portfolio,
transactions with tickets, scheduled events and an object store. ``push`` plays the
role of LEAN's data loop: it updates consolidators and indicators, then calls the
algorithm's ``on_data`` through the adapter. It is a test fixture: it does not model
fills beyond "fill at the bar's close", holidays, or Python.NET overloads.
"""

from collections import deque
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)


class Enum:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"Kind.{self.name}"

    def __repr__(self):
        return self.name

    def __eq__(self, other):
        return isinstance(other, Enum) and other.name == self.name

    def __hash__(self):
        return hash(self.name)


def enum(*names):
    return SimpleNamespace(**{n: Enum(n) for n in names})


Resolution = enum("MINUTE", "HOUR", "DAILY")
DataNormalizationMode = enum("RAW", "ADJUSTED", "SPLIT_ADJUSTED", "TOTAL_RETURN", "SCALED_RAW")
SecurityType = enum("EQUITY", "INDEX")
Market = SimpleNamespace(USA="usa")
TimeInForce = enum("DAY", "GOOD_TIL_CANCELED")
MovingAverageType = enum("WILDERS", "SIMPLE")
Field = SimpleNamespace(Open="open", High="high", Low="low", Close="close", Volume="volume")
OrderStatus = enum("NEW", "SUBMITTED", "FILLED", "CANCELED", "INVALID", "PARTIALLY_FILLED")


class SymbolId:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class Symbol:
    def __init__(self, ticker, kind=SecurityType.EQUITY):
        self.value = ticker
        self.id = SymbolId(f"{ticker} R735QTJ8XC9X")
        self.security_type = kind

    @staticmethod
    def create(ticker, kind, market):
        return Symbol(ticker, kind)

    def __hash__(self):
        return hash(self.id.value)

    def __eq__(self, other):
        return isinstance(other, Symbol) and other.id.value == self.id.value

    def __repr__(self):
        return f"Symbol({self.value})"


class TradeBar:
    def __init__(self, symbol, time, period, open, high, low, close, volume):
        self.symbol, self.time, self.period = symbol, time, period
        self.end_time = time + period
        self.open, self.high, self.low, self.close, self.volume = open, high, low, close, volume
        self.is_fill_forward = False


class Side:
    def __init__(self, close):
        self.close = close


class QuoteBar:
    def __init__(self, symbol, time, period, bid, ask):
        self.symbol, self.time, self.period = symbol, time, period
        self.end_time = time + period
        self.bid, self.ask = Side(bid), Side(ask)
        self.last_bid_size, self.last_ask_size = 100, 100


class _Generic:
    """``RollingWindow[TradeBar](n)`` style generics."""

    def __class_getitem__(cls, item):
        return cls


class RollingWindow(_Generic):
    def __init__(self, size):
        self.size = size
        self._items = deque(maxlen=size)

    def add(self, item):
        self._items.appendleft(item)

    @property
    def count(self):
        return len(self._items)

    @property
    def is_ready(self):
        return len(self._items) == self.size

    def __getitem__(self, index):
        return self._items[index]


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in self.handlers:
            handler(*args)


class CalendarInfo:
    def __init__(self, start, period):
        self.start, self.period = start, period


class TradeBarConsolidator:
    """Buckets bars by a fixed period, or by a calendar function's (start, period)."""

    def __init__(self, period):
        self.period = period
        self.data_consolidated = Event()
        self.current = None
        self.bucket_end = None

    def _bucket(self, bar):
        if callable(self.period):
            info = self.period(bar.time)
            return info.start, info.start + info.period
        midnight = bar.time.replace(hour=0, minute=0, second=0, microsecond=0)
        index = (bar.time - midnight) // self.period
        start = midnight + index * self.period
        return start, start + self.period

    def update(self, bar):
        start, end = self._bucket(bar)
        if self.current is not None and end != self.bucket_end:
            self.data_consolidated.fire(self, self.current)
            self.current = None
        if self.current is None:
            self.current = TradeBar(bar.symbol, start, end - start, bar.open, bar.high, bar.low,
                                    bar.close, bar.volume)
            self.bucket_end = end
        else:
            c = self.current
            c.high, c.low, c.close, c.volume = (
                max(c.high, bar.high), min(c.low, bar.low), bar.close, c.volume + bar.volume
            )

    def scan(self, local):
        if self.current is not None and local >= self.bucket_end:
            self.data_consolidated.fire(self, self.current)
            self.current = None


QuoteBarConsolidator = TradeBarConsolidator


class Hours:
    """Weekday sessions 09:30-16:00 in exchange local time, no holidays."""

    def _next(self, local, at, inclusive=False):
        candidate = datetime.combine(local.date(), at)
        if candidate < local or (candidate == local and not inclusive):
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    def get_next_market_close(self, local, extended):
        return self._next(local, CLOSE)

    def get_next_market_open(self, local, extended):
        return self._next(local, OPEN)

    def get_previous_market_open(self, local, extended):
        candidate = datetime.combine(local.date(), OPEN)
        while candidate > local or candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate

    def is_open(self, local, extended):
        return local.weekday() < 5 and OPEN <= local.time() < CLOSE

    def is_date_open(self, local):
        return local.weekday() < 5


class Security:
    def __init__(self, symbol):
        self.symbol = symbol
        self.exchange = SimpleNamespace(time_zone=SimpleNamespace(id="America/New_York"),
                                        hours=Hours())
        self.holdings = SimpleNamespace(quantity=0.0)
        self.price = 0.0
        self.symbol_properties = SimpleNamespace(lot_size=1.0, contract_multiplier=1.0)
        self.fundamentals = None
        self.shortable_provider = None
        self.quote_currency = SimpleNamespace(symbol="USD")
        self.models = []

    def set_slippage_model(self, model):
        self.models.append(model)

    def set_leverage(self, leverage):
        self.leverage = leverage


class Indicator:
    def __init__(self, kind, period, cadence, field=None):
        self.kind, self.period, self.cadence, self.field = kind, period, cadence, field
        self.window = RollingWindow(1)
        self.current = None
        self.samples = 0
        self._values = deque(maxlen=period)
        if kind == "dch":
            self.upper_band = SimpleNamespace(current=SimpleNamespace(value=0.0))
            self.lower_band = SimpleNamespace(current=SimpleNamespace(value=0.0))

    @property
    def is_ready(self):
        return self.samples >= self.period

    def update(self, bar):
        value = getattr(bar, self.field or "close")
        self._values.append(value)
        self.samples += 1
        if self.kind == "dch":
            self.upper_band.current.value = max(self._values)
            self.lower_band.current.value = min(self._values)
            value = (max(self._values) + min(self._values)) / 2
        else:
            value = sum(self._values) / len(self._values)
        self.current = SimpleNamespace(value=value, end_time=bar.end_time)
        if self.is_ready:
            self.window.add(self.current)


class Ticket:
    def __init__(self, order_id, symbol, quantity, status, tag, response=None):
        self.order_id, self.symbol, self.quantity = order_id, symbol, quantity
        self.status, self.tag, self.quantity_filled = status, tag, 0.0
        self.response = response

    def get_most_recent_order_response(self):
        return self.response


class Transactions:
    def __init__(self, algorithm):
        self.algorithm = algorithm
        self.orders = {}
        self.canceled = []

    def get_open_orders(self, symbol=None):
        return [
            SimpleNamespace(id=t.order_id, symbol=t.symbol, quantity=t.quantity, tag=t.tag)
            for t in self.orders.values()
            if t.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
            and (symbol is None or t.symbol == symbol)
        ]

    def get_order_ticket(self, order_id):
        return self.orders[order_id]

    def get_order_by_id(self, order_id):
        ticket = self.orders[order_id]
        return SimpleNamespace(id=order_id, symbol=ticket.symbol, tag=ticket.tag)

    def cancel_order(self, order_id, tag=""):
        ticket = self.orders[order_id]
        ticket.status = OrderStatus.CANCELED
        self.canceled.append(order_id)
        self.algorithm._emit_order_event(ticket, OrderStatus.CANCELED)
        return ticket


class Slice:
    def __init__(self, bars=(), quote_bars=(), custom=None, dividends=None, splits=None,
                 delistings=None, symbol_changed_events=None):
        self.bars = {b.symbol: b for b in bars}
        self.quote_bars = {b.symbol: b for b in quote_bars}
        self._custom = custom or {}
        self.dividends = dividends or {}
        self.splits = splits or {}
        self.delistings = delistings or {}
        self.symbol_changed_events = symbol_changed_events or {}

    def get(self, kind):
        return self._custom.get(kind)


class FakeAlgorithm:
    """The subset of QCAlgorithm the adapter touches, with a driver (``push``)."""

    def __init__(self, start=datetime(2025, 1, 6, 9, 30), history=None, fills=True):
        self.time = start  # algorithm-local time, New York
        self.settings = SimpleNamespace()
        self.securities = {}
        self.initializers = []
        self.consolidators = {}
        self.indicators = []
        self.universe_settings = SimpleNamespace(schedule=SimpleNamespace(on=lambda rule: None))
        self.universes = []
        self.date_rules = SimpleNamespace(
            every_day=lambda *a: ("every_day", a), month_start=lambda *a: ("month_start", a),
            week_start=lambda *a: ("week_start", a), month_end=lambda *a: ("month_end", a),
            year_start=lambda *a: ("year_start", a),
        )
        self.time_rules = SimpleNamespace(
            after_market_open=lambda s, m: ("after_open", s, m),
            before_market_close=lambda s, m: ("before_close", s, m),
            at=lambda h, m, z=None: ("at", h, m, z),
        )
        self.scheduled = []
        self.schedule = SimpleNamespace(on=lambda d, t, fn: self.scheduled.append((d, t, fn)))
        self.warm_up_request = None
        self.is_warming_up = False
        self.histories = history or {}
        self.cash = 100_000.0
        self.transactions = Transactions(self)
        self.order_events = []
        self.fills = fills
        self._next_order = 0
        self.saved = {}
        self.object_store = SimpleNamespace(save=lambda k, v: self.saved.__setitem__(k, v))
        self.messages = []
        self.project_id, self.algorithm_id = 1, "run"
        self.account_currency = "USD"
        self.adapter = None
        self.custom_data = []
        self.deregistered = []
        self.registered = []
        self.warmed = []

    # ---- QCAlgorithm surface ----

    @property
    def utc_time(self):
        return self.time.replace(tzinfo=NEW_YORK).astimezone(UTC).replace(tzinfo=None)

    @property
    def portfolio(self):
        algorithm = self

        class Portfolio:
            @property
            def total_portfolio_value(self):
                return algorithm.cash + sum(
                    s.holdings.quantity * s.price for s in algorithm.securities.values()
                )

            @property
            def cash(self):
                return algorithm.cash

            def __iter__(self):
                for security in algorithm.securities.values():
                    yield SimpleNamespace(
                        Value=SimpleNamespace(
                            invested=security.holdings.quantity != 0,
                            symbol=security.symbol,
                            quantity=security.holdings.quantity,
                            price=security.price,
                        )
                    )

        return Portfolio()

    def add_security_initializer(self, fn):
        self.initializers.append(fn)

    def add_security(self, symbol, resolution, **settings):
        security = self.securities.get(symbol)
        if security is None:
            security = Security(symbol)
            security.resolution, security.settings = resolution, settings
            for fn in self.initializers:
                fn(security)
            self.securities[symbol] = security
        return security

    def add_data(self, kind, name):
        symbol = Symbol(name)
        self.custom_data.append(kind)
        return SimpleNamespace(symbol=symbol)

    def add_universe(self, universe):
        self.universes.append(universe)
        return universe

    @property
    def universe(self):
        return SimpleNamespace(etf=lambda ticker, settings, selector: ("etf", ticker, selector))

    @property
    def subscription_manager(self):
        algorithm = self

        class Manager:
            def add_consolidator(self, symbol, consolidator):
                algorithm.consolidators.setdefault(symbol, []).append(consolidator)

            def remove_consolidator(self, symbol, consolidator):
                algorithm.consolidators[symbol].remove(consolidator)

        return Manager()

    def _indicator(self, kind, symbol, period, cadence, field=None):
        # LEAN's Python helpers bind only a Resolution here; a timedelta is refused.
        if not isinstance(cadence, Enum):
            raise TypeError("No method matches given arguments: resolution expected Resolution")
        indicator = Indicator(kind, period, cadence, field)
        self.indicators.append((symbol, indicator))
        return indicator

    def register_indicator(self, symbol, indicator, consolidator, selector=None):
        indicator.field = selector
        consolidator.data_consolidated += lambda sender, bar: indicator.update(bar)
        self.subscription_manager.add_consolidator(symbol, consolidator)
        self.registered.append((symbol, indicator, consolidator))

    def warm_up_indicator(self, symbol, indicator, period, selector=None):
        if not isinstance(period, (timedelta, Enum)):
            raise TypeError("warm_up_indicator takes a Resolution or a timedelta")
        self.warmed.append((symbol, indicator, period))

    def sma(self, symbol, period, cadence, field=None):
        return self._indicator("sma", symbol, period, cadence, field)

    def ema(self, symbol, period, *args):
        cadence, field = (args[0], args[1]) if len(args) == 2 else (args[1], args[2])
        return self._indicator("ema", symbol, period, cadence, field)

    def rsi(self, symbol, period, average, cadence, field=None):
        return self._indicator("rsi", symbol, period, cadence, field)

    def dch(self, symbol, period, cadence):
        return self._indicator("dch", symbol, period, cadence)

    def deregister_indicator(self, indicator):
        self.deregistered.append(indicator)

    def set_warm_up(self, *args):
        self.warm_up_request = args

    @property
    def history(self):
        algorithm = self

        class History(_Generic):
            def __getitem__(self, item):
                return self

            def __call__(self, symbols, *args, **kwargs):
                count = args[0] if isinstance(args[0], int) else None
                slices = []
                bars_by_symbol = {
                    s: [
                        b for b in algorithm.histories.get(s.value, ())
                        if b.end_time <= algorithm.time
                    ]
                    for s in symbols
                }
                if count is not None:
                    bars_by_symbol = {s: b[-count:] for s, b in bars_by_symbol.items()}
                else:
                    start = args[0]
                    bars_by_symbol = {
                        s: [x for x in b if x.time >= start] for s, b in bars_by_symbol.items()
                    }
                ends = sorted({b.end_time for bars in bars_by_symbol.values() for b in bars})
                for end in ends:
                    slices.append({s: next(b for b in bars if b.end_time == end)
                                   for s, bars in bars_by_symbol.items()
                                   if any(b.end_time == end for b in bars)})
                return slices

        return History()

    def market_order(self, symbol, quantity, asynchronous=False, tag="", order_properties=None):
        self._next_order += 1
        order_id = self._next_order
        if quantity > 10_000:
            response = SimpleNamespace(error_code="TooBig", error_message="order too large")
            ticket = Ticket(order_id, symbol, quantity, OrderStatus.INVALID, tag, response)
            self.transactions.orders[order_id] = ticket
            return ticket
        ticket = Ticket(order_id, symbol, quantity, OrderStatus.SUBMITTED, tag)
        self.transactions.orders[order_id] = ticket
        if self.fills:
            self._fill(ticket)
        return ticket

    market_on_open_order = market_order
    market_on_close_order = market_order

    def _fill(self, ticket):
        security = self.securities[ticket.symbol]
        security.holdings.quantity += ticket.quantity
        self.cash -= ticket.quantity * security.price
        ticket.quantity_filled = ticket.quantity
        ticket.status = OrderStatus.FILLED
        self._emit_order_event(ticket, OrderStatus.FILLED)

    def _emit_order_event(self, ticket, status):
        event = SimpleNamespace(order_id=ticket.order_id, symbol=ticket.symbol, status=status,
                                message="", utc_time=self.utc_time)
        self.order_events.append(event)
        if self.adapter is not None:
            self.adapter.on_order_event(event)

    def debug(self, message):
        self.messages.append(message)

    log = debug

    # ---- the data loop ----

    def push(self, local_time, slice):
        """Advance the clock, update what LEAN would update, deliver the slice."""
        self.time = local_time
        for bar in slice.bars.values():
            security = self.securities.get(bar.symbol)
            if security is not None:
                security.price = bar.close
            for consolidator in self.consolidators.get(bar.symbol, ()):
                consolidator.update(bar)
            for symbol, indicator in self.indicators:
                if symbol == bar.symbol and indicator.cadence == self.securities[symbol].resolution:
                    indicator.update(bar)
        self.adapter.on_data(slice)

    def changed(self, added=(), removed=()):
        self.adapter.on_securities_changed(
            SimpleNamespace(added_securities=[self.securities[s] for s in added],
                            removed_securities=[self.securities[s] for s in removed])
        )


def _manual(kind):
    class Manual(Indicator):
        def __init__(self, name, period, *rest):
            super().__init__(kind, period, None)
            self.name = name

    return Manual


SimpleMovingAverage = _manual("sma")
ExponentialMovingAverage = _manual("ema")
RelativeStrengthIndex = _manual("rsi")
DonchianChannel = _manual("dch")


class FundamentalUniverse:
    @staticmethod
    def usa(selector, settings):
        return ("fundamental", selector, settings)


class UniverseSettings:
    def __init__(self, other):
        self.schedule = SimpleNamespace(rules=[], on=lambda rule: self.schedule.rules.append(rule))


LEAN = SimpleNamespace(
    Resolution=Resolution,
    DataNormalizationMode=DataNormalizationMode,
    SecurityType=SecurityType,
    Market=Market,
    Symbol=Symbol,
    TradeBar=TradeBar,
    QuoteBar=QuoteBar,
    RollingWindow=RollingWindow,
    TradeBarConsolidator=TradeBarConsolidator,
    QuoteBarConsolidator=QuoteBarConsolidator,
    CalendarInfo=CalendarInfo,
    ConstantSlippageModel=lambda rate: ("slippage", rate),
    InteractiveBrokersShortableProvider=lambda: "ib",
    ShortMarginInterestRateModel=lambda: "margin",
    OrderProperties=lambda: SimpleNamespace(),
    TimeInForce=TimeInForce,
    MovingAverageType=MovingAverageType,
    Field=Field,
    Universe=SimpleNamespace(UNCHANGED="unchanged"),
    UniverseSettings=UniverseSettings,
    FundamentalUniverse=FundamentalUniverse,
    SimpleMovingAverage=SimpleMovingAverage,
    ExponentialMovingAverage=ExponentialMovingAverage,
    RelativeStrengthIndex=RelativeStrengthIndex,
    DonchianChannel=DonchianChannel,
    EODHDUpcomingEarnings="EODHDUpcomingEarnings",
)


def daily_bar(symbol, day: date, close, open=None):
    """A daily bar for a New York session: time 09:30, ends 16:00."""
    start = datetime.combine(day, OPEN)
    open = close if open is None else open
    return TradeBar(symbol, start, timedelta(hours=6, minutes=30), open, max(open, close) + 1,
                    min(open, close) - 1, close, 1000.0)


def minute_bar(symbol, start: datetime, close):
    return TradeBar(symbol, start, timedelta(minutes=1), close, close + 0.5, close - 0.5, close,
                    100.0)
