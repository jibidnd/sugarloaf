"""An adapter driven by the caller, with the same interface as a platform.

The test (or example) hands bars, quotes, custom records, membership changes and
scheduled times to ``deliver``, ``set_members`` and ``fire``; the adapter does what
a platform would (consolidates, keeps windows, updates indicators, reports the
account, sizes orders) and forwards the callback to the runner. Unless ``fills``
is False, it fills every order immediately at the last price given. It charges no
fee or slippage, has no session calendar unless one is given, and has no data it
was not handed.
"""

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sugarloaf.adapters.memory import indicators as formulas
from sugarloaf.adapters.protocol import Arrival, IndicatorReading
from sugarloaf.core.data import (
    Bar,
    Delisting,
    Dividend,
    QuoteBar,
    Split,
    SymbolChange,
    Window,
)
from sugarloaf.core.errors import CapabilityError, ContractError, NotReady
from sugarloaf.core.inputs import (
    Bars,
    Custom,
    HistoryRequest,
    Indicator,
    Quotes,
    Schedule,
    Universe,
    WarmUp,
)
from sugarloaf.core.primitives import InstrumentId, aware
from sugarloaf.core.universes import select
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


@dataclass(frozen=True)
class Sessions:
    """An exchange calendar given as a tuple of (open, close) pairs; extended hours optional."""

    regular: tuple[tuple[datetime, datetime], ...]
    extended: tuple[tuple[datetime, datetime], ...] = ()

    def _sessions(self, extended: bool):
        return self.extended if extended and self.extended else self.regular

    def next_close(self, after: datetime, count: int = 1) -> datetime:
        closes = sorted(end for _, end in self.regular if end > after)
        if len(closes) < count:
            raise NotReady("The given calendar has no such later session")
        return closes[count - 1]

    def next_open(self, after: datetime, count: int = 1) -> datetime:
        opens = sorted(start for start, _ in self.regular if start > after)
        if len(opens) < count:
            raise NotReady("The given calendar has no such later session")
        return opens[count - 1]

    def is_open(self, at: datetime, extended: bool = False) -> bool:
        return any(start <= at < end for start, end in self._sessions(extended))

    def session(self, at: datetime, extended: bool = False) -> tuple[datetime, datetime]:
        started = [(s, e) for s, e in self._sessions(extended) if s <= at]
        if not started:
            raise NotReady("The given calendar has no session at or before this time")
        return max(started)


class _Consolidator:
    """Bucket feed bars into interval bars; daily buckets follow the bar's date."""

    def __init__(self, interval: timedelta):
        self.interval = interval
        self.current: Bar | None = None
        self.bucket = None

    def _bucket(self, bar: Bar):
        if self.interval == timedelta(days=1):
            return bar.start.date()
        seconds = (bar.start - bar.start.replace(hour=0, minute=0, second=0, microsecond=0))
        return bar.start.date(), int(seconds // self.interval)

    def update(self, bar: Bar) -> Bar | None:
        """Add a feed bar; return the completed interval bar when a new bucket starts."""
        bucket = self._bucket(bar)
        completed = None
        if self.current is not None and bucket != self.bucket:
            completed = self.current
            self.current = None
        if self.current is None:
            self.current, self.bucket = bar, bucket
        else:
            self.current = replace(
                self.current,
                end=bar.end,
                high=max(self.current.high, bar.high),
                low=min(self.current.low, bar.low),
                close=bar.close,
                volume=self.current.volume + bar.volume,
            )
        return completed

    def flush(self) -> Bar | None:
        completed, self.current = self.current, None
        return completed


class MemoryAdapter:
    """An adapter that replays the bars, membership changes and scheduled times the caller
    hands it, keeps windows and indicators from them, and fills orders at the price last given.

    Args:
        start: The clock before the first delivery.
        account: Starting NAV in account currency (all cash).
        calendars: Exchange sessions by instrument.
        histories: Past daily bars by instrument, for ``history`` requests.
        lot_sizes: Lot size by instrument, default one.
        fills: Fill every order immediately at the current mark.
    """

    def __init__(
        self,
        start: datetime,
        *,
        account: float = 100_000.0,
        calendars: Mapping[InstrumentId, Sessions] | None = None,
        histories: Mapping[InstrumentId, Sequence[Bar]] | None = None,
        lot_sizes: Mapping[InstrumentId, float] | None = None,
        fills: bool = True,
    ):
        aware(start)
        self.runner = None
        self.clock = start
        self.cash = float(account)
        self.holdings: dict[InstrumentId, float] = {}
        self.prices: dict[InstrumentId, float] = {}
        self.calendars = dict(calendars or {})
        self.histories = {k: tuple(sorted(v, key=lambda b: b.end)) for k, v in
                          (histories or {}).items()}
        self.lot_sizes = dict(lot_sizes or {})
        self.fills = fills
        # Declaration key -> instrument -> newest-last deque of completed bars.
        self.windows: dict[tuple, dict[InstrumentId, deque]] = {}
        self.window_sizes: dict[tuple, int] = {}
        self.consolidators: dict[tuple, dict[InstrumentId, _Consolidator]] = {}
        self.feeds: dict[tuple, Bars | Quotes] = {}
        self.indicators: dict[tuple[Indicator, InstrumentId], object] = {}
        self.universes: dict[str, Universe] = {}
        self.universe_feeds: dict[str, tuple] = {}
        self.membership: dict[str, tuple[InstrumentId, ...]] = {}
        self.customs: set[Custom] = set()
        self.schedules: dict[str, Schedule] = {}
        self.warm_up_request: WarmUp | None = None
        self.open_orders: list[OpenOrder] = []
        self.saved: dict[str, str] = {}
        self._order_sequence = 0

    # ------------------------------------------------------------------ setup

    def attach(self, runner) -> None:
        """Bind the runner that receives callbacks, then set up its declarations."""
        self.runner = runner
        runner.start()

    def subscribe(self, declaration, instruments) -> None:
        key = declaration.key
        self.feeds[key] = declaration
        size = max(self.window_sizes.get(key, 0), declaration.window)
        if size != self.window_sizes.get(key):
            # A wider window asked for later enlarges the shared deque.
            self.window_sizes[key] = size
            for instrument, bars in self.windows.get(key, {}).items():
                self.windows[key][instrument] = deque(bars, maxlen=size)
        for instrument in instruments:
            self._track(key, instrument)

    def _track(self, key, instrument) -> None:
        windows = self.windows.setdefault(key, {})
        if instrument not in windows:
            windows[instrument] = deque(maxlen=self.window_sizes[key])
        if self.feeds[key].interval is not None:
            self.consolidators.setdefault(key, {}).setdefault(
                instrument, _Consolidator(self.feeds[key].interval)
            )

    def add_universe(self, definition, feeds) -> None:
        self.universes[definition.name] = definition
        self.universe_feeds[definition.name] = tuple(feeds)
        for feed in feeds:
            self.subscribe(feed, ())
        self.membership.setdefault(definition.name, ())

    def add_custom(self, declaration) -> None:
        self.customs.add(declaration)

    def add_indicator(self, declaration, instruments) -> None:
        for instrument in instruments:
            self.indicators.setdefault((declaration, instrument), formulas.create(declaration))

    def schedule(self, declaration) -> None:
        self.schedules[declaration.name] = declaration

    def warm_up(self, declaration) -> None:
        self.warm_up_request = declaration

    # ---------------------------------------------------------------- driving

    def set_members(
        self, universe: str, members: Iterable[InstrumentId], warming_up: bool = False
    ) -> None:
        """Replace a universe's members, subscribing the new ones, and notify the runner.

        ``warming_up`` marks a change made during a warm-up replay, as for ``deliver``.
        """
        if universe not in self.universes:
            raise ContractError(f"No universe named {universe}")
        members = tuple(dict.fromkeys(members))
        before = self.membership.get(universe, ())
        added = tuple(i for i in members if i not in before)
        removed = tuple(i for i in before if i not in members)
        self.membership[universe] = members
        for feed in self.universe_feeds[universe]:
            for instrument in added:
                self._track(feed.key, instrument)
        for (declaration, instrument) in list(self.indicators):
            if declaration.selection.universe == universe and instrument in removed:
                del self.indicators[(declaration, instrument)]
        if self.runner is not None:
            declarations = {d for d, _ in self.indicators} | self._universe_indicators(universe)
            for declaration in declarations:
                if declaration.selection.universe == universe:
                    for instrument in added:
                        self.indicators.setdefault(
                            (declaration, instrument), formulas.create(declaration)
                        )
            if added or removed:
                self.runner.on_securities_changed(universe, added, removed, warming_up)

    def _universe_indicators(self, universe: str) -> set:
        result = set()
        if self.runner is None:
            return result
        for node_indicators in self.runner._indicators.values():
            for declaration in node_indicators.values():
                if declaration.selection.universe == universe:
                    result.add(declaration)
        return result

    def refresh(self, universe: str, rows, at: datetime | None = None) -> None:
        """Select members from the given selection rows with the shared rule, then set them."""
        self.set_members(universe, select(self.universes[universe], rows, at or self.clock))

    def deliver(
        self,
        time: datetime,
        bars: Iterable[Bar] = (),
        quotes: Iterable[QuoteBar] = (),
        custom: Mapping[str, Iterable] | None = None,
        dividends: Iterable[Dividend] = (),
        splits: Iterable[Split] = (),
        delistings: Iterable[Delisting] = (),
        symbol_changes: Iterable[SymbolChange] = (),
        warming_up: bool = False,
    ) -> Arrival:
        """Advance the clock, route feed bars to every matching feed, call the runner.

        A bar is routed to each subscribed feed whose resolution period equals the
        bar's duration and that tracks the instrument; feeds with an interval
        consolidate it and deliver the interval bar when it completes.
        """
        aware(time)
        if time < self.clock:
            raise ContractError("The memory adapter's clock does not run backwards")
        self.clock = time
        delivered_bars: dict[tuple, dict] = {}
        delivered_quotes: dict[tuple, dict] = {}
        for bar in bars:
            self.prices[bar.instrument] = bar.close
            for key, feed in self.feeds.items():
                if not isinstance(feed, Bars) or bar.instrument not in self.windows.get(key, {}):
                    continue
                if bar.end - bar.start != _period(feed.resolution):
                    continue
                completed = bar
                if feed.interval is not None:
                    completed = self.consolidators[key][bar.instrument].update(bar)
                if completed is None:
                    continue
                self.windows[key][bar.instrument].append(completed)
                delivered_bars.setdefault(key, {})[bar.instrument] = completed
                for (declaration, instrument), state in self.indicators.items():
                    if instrument == bar.instrument and declaration.bars.key == key:
                        state.update(completed)
        for quote in quotes:
            for key, feed in self.feeds.items():
                if isinstance(feed, Quotes) and quote.instrument in self.windows.get(key, {}):
                    self.windows[key][quote.instrument].append(quote)
                    delivered_quotes.setdefault(key, {})[quote.instrument] = quote
        arrival = Arrival(
            time,
            delivered_bars,
            delivered_quotes,
            {k: tuple(v) for k, v in (custom or {}).items()},
            tuple(dividends),
            tuple(splits),
            tuple(delistings),
            tuple(symbol_changes),
            warming_up,
        )
        if self.runner is not None:
            self.runner.on_data(arrival)
        return arrival

    def fire(self, name: str, time: datetime | None = None, warming_up: bool = False) -> None:
        """Fire a declared schedule at ``time`` (default: the current clock).

        ``warming_up`` marks a call made during a warm-up replay, as for ``deliver``.
        """
        if name not in self.schedules:
            raise ContractError(f"No schedule named {name}")
        if time is not None:
            aware(time)
            self.clock = max(self.clock, time)
        self.runner.on_scheduled(name, warming_up)

    def set_price(self, instrument: InstrumentId, price: float) -> None:
        self.prices[instrument] = float(price)

    # ---------------------------------------------------------------- mapping

    @property
    def time(self) -> datetime:
        return self.clock

    def window(self, declaration, instrument):
        bars = self.windows.get(declaration.key, {}).get(instrument)
        if bars is None:
            raise CapabilityError(f"{instrument.value} is not subscribed for this feed")
        return Window(tuple(reversed(bars))[: declaration.window])

    def indicator(self, declaration, instrument) -> IndicatorReading:
        state = self.indicators.get((declaration, instrument))
        if state is None:
            raise CapabilityError(f"No {declaration.name} indicator for {instrument.value}")
        return IndicatorReading(
            state.is_ready, state.value, state.time, tuple(state.values), dict(state.outputs)
        )

    def members(self, universe: str) -> tuple[InstrumentId, ...]:
        if universe not in self.universes:
            raise CapabilityError(f"No universe named {universe}")
        return self.membership.get(universe, ())

    def history(self, request: HistoryRequest):
        result = {}
        for instrument in request.instruments:
            bars = [b for b in self.histories.get(instrument, ()) if b.end <= self.clock]
            if request.count is not None:
                bars = bars[-request.count:]
            else:
                bars = [b for b in bars if b.end > self.clock - request.span]
            result[instrument] = tuple(bars)
        return result

    def calendar(self, instrument: InstrumentId):
        try:
            return self.calendars[instrument]
        except KeyError:
            raise CapabilityError(f"No calendar was given for {instrument.value}") from None

    def portfolio(self) -> Portfolio:
        value = self.cash + sum(
            q * self.prices.get(i, 0.0) for i, q in self.holdings.items()
        )
        return Portfolio(
            self.clock, value, self.cash, dict(self.holdings), dict(self.prices),
            tuple(self.open_orders),
        )

    def execute(self, target: PositionView, policy: ExecutionPolicy) -> tuple[OrderRecord, ...]:
        account = self.portfolio()
        records = []
        target_id = target.metadata.view_id
        if policy.cancel_on_new_target:
            # Open orders of an older target in the target's instruments are cancelled;
            # this target's own open orders stay and count as pending below.
            kept = []
            for order in self.open_orders:
                if order.instrument in target.positions and order.tag != target_id:
                    self._report(order, "canceled")
                else:
                    kept.append(order)
            self.open_orders = kept
        for instrument in sorted(target.positions, key=lambda i: i.value):
            amount = target.positions[instrument]
            price = self.prices.get(instrument)
            if price is None or price <= 0:
                records.append(OrderRecord(target_id, instrument, 0.0, None, "skipped", "no mark"))
                continue
            whole, exact = desired_quantity(
                amount, target.units, price, account.nav, self.lot_sizes.get(instrument, 1.0)
            )
            # Pending is read after the cancellations above, from what is still open.
            pending = sum(o.remaining for o in self.open_orders if o.instrument == instrument)
            quantity = order_quantity(
                whole, exact, self.holdings.get(instrument, 0.0), pending,
                price, account.nav, target.units, policy,
            )
            if quantity == 0:
                continue
            self._order_sequence += 1
            order = OpenOrder(str(self._order_sequence), instrument, quantity, 0.0, target_id)
            if self.fills:
                self.holdings[instrument] = self.holdings.get(instrument, 0.0) + quantity
                self.cash -= quantity * price
                if self.holdings[instrument] == 0:
                    del self.holdings[instrument]
                self._report(replace(order, filled=quantity), "filled")
                status = "filled"
            else:
                self.open_orders.append(order)
                status = "submitted"
            records.append(OrderRecord(target_id, instrument, quantity, order.order_id, status))
        return tuple(records)

    def fill(self, order_id: str) -> None:
        """Fill one open order at the current mark (for runs created with fills=False)."""
        for order in self.open_orders:
            if order.order_id == order_id:
                break
        else:
            raise ContractError(f"No open order {order_id}")
        self.open_orders.remove(order)
        price = self.prices[order.instrument]
        self.holdings[order.instrument] = self.holdings.get(order.instrument, 0.0) + order.quantity
        self.cash -= order.quantity * price
        self._report(replace(order, filled=order.quantity), "filled")

    def _report(self, order: OpenOrder, status: str) -> None:
        if self.runner is not None:
            self.runner.on_order_event(
                OrderUpdate(order.order_id, order.instrument, status, order.filled, self.clock,
                            order.tag)
            )

    def save(self, key: str, text: str) -> None:
        self.saved[key] = text


def _period(resolution: str) -> timedelta:
    from sugarloaf.core.inputs import RESOLUTION_PERIODS

    return RESOLUTION_PERIODS[resolution]
