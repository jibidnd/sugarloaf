"""The adapter interface: setup methods for declarations, mapping methods for reads.

An adapter maps each Sugarloaf construct to one platform call. It owns no graph
logic: the runner decides which node runs; the adapter subscribes, consolidates,
registers, schedules, reads and orders through the platform's own API, and
forwards the platform's callbacks to the runner as ``Arrival`` records.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sugarloaf.core.data import (
    Bar,
    Delisting,
    Dividend,
    QuoteBar,
    Split,
    SymbolChange,
)
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
from sugarloaf.core.primitives import InstrumentId, aware, frozen_map
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import ExecutionPolicy, OrderRecord, Portfolio


@dataclass(frozen=True)
class Arrival:
    """One platform callback's records, before the runner splits them per node.

    ``bars`` and ``quotes`` are keyed by the declaration key (``Bars.key``) and then
    by instrument, so two consolidations of one instrument stay apart. ``custom``
    is keyed by ``Custom.kind``. ``warming_up`` marks records replayed before the
    start date.
    """

    time: datetime
    bars: Mapping[tuple, Mapping[InstrumentId, Bar]] = field(default_factory=dict)
    quotes: Mapping[tuple, Mapping[InstrumentId, QuoteBar]] = field(default_factory=dict)
    custom: Mapping[str, tuple] = field(default_factory=dict)
    dividends: tuple[Dividend, ...] = ()
    splits: tuple[Split, ...] = ()
    delistings: tuple[Delisting, ...] = ()
    symbol_changes: tuple[SymbolChange, ...] = ()
    warming_up: bool = False

    def __post_init__(self):
        aware(self.time)
        object.__setattr__(
            self, "bars", frozen_map({k: frozen_map(v) for k, v in self.bars.items()})
        )
        object.__setattr__(
            self, "quotes", frozen_map({k: frozen_map(v) for k, v in self.quotes.items()})
        )
        object.__setattr__(
            self, "custom", frozen_map({k: tuple(v) for k, v in self.custom.items()})
        )
        for name in ("dividends", "splits", "delistings", "symbol_changes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class IndicatorReading:
    """A native indicator as read now: readiness, current value and recent values.

    ``values`` are (time, value) pairs newest first, at most the declared window.
    ``outputs`` carries named bands for multi-output indicators such as Donchian.
    """

    is_ready: bool
    value: float | None
    time: datetime | None
    values: tuple[tuple[datetime, float], ...] = ()
    outputs: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "values", tuple(self.values))
        object.__setattr__(self, "outputs", frozen_map(self.outputs))


class Calendar(Protocol):
    """An instrument's exchange sessions, from the platform's calendar."""

    def next_close(self, after: datetime, count: int = 1) -> datetime:
        """The close of the count-th regular session ending strictly after ``after``."""
        ...

    def next_open(self, after: datetime, count: int = 1) -> datetime:
        """The open of the count-th regular session starting strictly after ``after``."""
        ...

    def is_open(self, at: datetime, extended: bool = False) -> bool: ...

    def session(self, at: datetime, extended: bool = False) -> tuple[datetime, datetime]:
        """Open and close of the session containing ``at``, or the latest one before it."""
        ...


class Adapter(Protocol):
    """Setup and mapping over one platform.

    Setup methods are called once by the runner, before the platform's clock
    starts, in the order below; ``instruments`` are the explicit instruments of the
    declaration's selection. Universe members are subscribed by the platform with
    the universe's feed settings, and the adapter creates their windows and
    indicators when it reports them added.
    """

    def subscribe(self, declaration: Bars | Quotes, instruments: Sequence[InstrumentId]) -> None:
        """Subscribe the feed, consolidate to the interval, keep the window."""
        ...

    def add_universe(self, definition: Universe, feeds: Sequence[Bars | Quotes]) -> None:
        """Create the membership rule; members get the feeds that select the universe."""
        ...

    def add_custom(self, declaration: Custom) -> None: ...

    def add_indicator(self, declaration: Indicator, instruments: Sequence[InstrumentId]) -> None:
        """Create and warm the native indicator per instrument."""
        ...

    def schedule(self, declaration: Schedule) -> None: ...

    def warm_up(self, declaration: WarmUp) -> None: ...

    @property
    def time(self) -> datetime:
        """The platform clock, aware UTC."""
        ...

    def window(self, declaration: Bars | Quotes, instrument: InstrumentId):
        """Newest-first rolling window of completed bars; empty before any completed."""
        ...

    def indicator(self, declaration: Indicator, instrument: InstrumentId) -> IndicatorReading: ...

    def members(self, universe: str) -> tuple[InstrumentId, ...]: ...

    def history(self, request: HistoryRequest) -> Mapping[InstrumentId, tuple[Bar, ...]]:
        """Past bars ending at or before now, oldest first per instrument."""
        ...

    def calendar(self, instrument: InstrumentId) -> Calendar: ...

    def portfolio(self) -> Portfolio: ...

    def execute(self, target: PositionView, policy: ExecutionPolicy) -> tuple[OrderRecord, ...]:
        """Turn a position target into platform orders, by the shared sizing rule."""
        ...
