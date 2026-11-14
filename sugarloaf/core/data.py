"""The neutral records an adapter hands to nodes, and the slice that carries them.

Every record is immutable and timestamped with an aware UTC time. A ``Slice`` is one
callback's worth of them, restricted to what the receiving node declared. The
adapter translates platform objects field by field; nothing is filtered by time,
because the platform delivers each record once, when it is known.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, aware, finite, frozen_map


@dataclass(frozen=True)
class Bar:
    """A completed trade bar. ``end`` is when it completed, aware UTC."""

    instrument: InstrumentId
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    # True when the platform repeated the previous bar because nothing traded.
    fill_forward: bool = False

    def __post_init__(self):
        aware(self.start)
        aware(self.end)
        finite(self.open, self.high, self.low, self.close, self.volume)
        # Prices may be zero or negative (back-adjusted series are), so the sign is
        # not checked; the range must still contain the open and the close.
        if (
            self.start >= self.end
            or self.volume < 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
        ):
            raise ContractError("Invalid completed bar")


@dataclass(frozen=True)
class QuoteBar:
    """A completed bid/ask bar; closes of each side, with the last sizes."""

    instrument: InstrumentId
    start: datetime
    end: datetime
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None

    def __post_init__(self):
        aware(self.start)
        aware(self.end)
        finite(self.bid, self.ask)
        if self.start >= self.end or self.bid <= 0 or self.ask < self.bid:
            raise ContractError("Invalid quote bar")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Dividend:
    """A cash dividend going ex at ``time``; ``amount`` per share."""

    instrument: InstrumentId
    time: datetime
    amount: float
    reference_price: float | None = None

    def __post_init__(self):
        aware(self.time)
        finite(self.amount, *(() if self.reference_price is None else (self.reference_price,)))


@dataclass(frozen=True)
class Split:
    """An effective split at ``time``. ``factor`` multiplies the old price: 0.1 for 10-for-1."""

    instrument: InstrumentId
    time: datetime
    factor: float
    reference_price: float | None = None

    def __post_init__(self):
        aware(self.time)
        finite(self.factor)
        if self.factor <= 0:
            raise ContractError("Split requires a positive price factor")


@dataclass(frozen=True)
class Delisting:
    """The instrument stops trading at ``time``."""

    instrument: InstrumentId
    time: datetime


@dataclass(frozen=True)
class SymbolChange:
    """The instrument's ticker changed; its identity did not."""

    instrument: InstrumentId
    time: datetime
    old_ticker: str
    new_ticker: str


EARNINGS_TIMINGS = ("before_open", "after_close", "unknown")


@dataclass(frozen=True)
class EarningsListing:
    """An earnings calendar entry as published at ``time``.

    ``report_date`` is the session of the report; ``timing`` says whether it falls
    before that session's open or after its close. A moved date appears as a newer
    listing with the new date.
    """

    instrument: InstrumentId
    time: datetime
    report_date: date
    timing: str = "unknown"

    def __post_init__(self):
        aware(self.time)
        if self.timing not in EARNINGS_TIMINGS:
            raise ContractError(f"Unknown earnings timing: {self.timing}")


@dataclass(frozen=True)
class FundamentalRecord:
    """A security's company fundamentals as the platform reported them at ``time``.

    ``cik`` is the issuer's SEC Central Index Key as text without leading zeros.
    ``sector`` is the Morningstar sector code (101 basic materials, 102 consumer
    cyclical, 103 financial services, 104 real estate, 205 consumer defensive,
    206 healthcare, 207 utilities, 308 communication services, 309 energy,
    310 industrials, 311 technology). Fields the platform lacks are None.
    """

    instrument: InstrumentId
    time: datetime
    cik: str | None = None
    market_cap: float | None = None
    price: float | None = None
    dollar_volume: float | None = None
    sector: int | None = None

    def __post_init__(self):
        aware(self.time)
        finite(*(x for x in (self.market_cap, self.price, self.dollar_volume) if x is not None))
        if self.cik is not None:
            cik = str(self.cik).strip().lstrip("0")
            if cik and not cik.isdigit():
                raise ContractError("A CIK is a number")
            object.__setattr__(self, "cik", cik or None)


@dataclass(frozen=True)
class BorrowTerms:
    """Borrow fee (annual, decimal) and shortable quantity reported at ``time``.

    None in a field means the platform reports no limit for it. A platform without
    real borrow data gives no record at all, so missing data cannot pass as free
    unlimited borrow.
    """

    instrument: InstrumentId
    time: datetime
    fee_rate: float | None = None
    shortable: float | None = None

    def __post_init__(self):
        aware(self.time)
        finite(*(x for x in (self.fee_rate, self.shortable) if x is not None))


@dataclass(frozen=True)
class Observation:
    """A finite scalar observed at ``time``, used by research outcome resolvers."""

    time: datetime
    value: float
    available_at: datetime | None = None

    def __post_init__(self):
        aware(self.time)
        finite(self.value)
        if self.available_at is None:
            object.__setattr__(self, "available_at", self.time)
        aware(self.available_at)
        if self.available_at < self.time:
            raise ContractError("An observation cannot be available before its time")


@dataclass(frozen=True)
class Slice:
    """One callback's records for one node, keyed by instrument.

    ``bars`` holds the bars of the node's unnamed ``Bars`` declarations, ``quotes``
    those of its unnamed ``Quotes`` declarations; ``series(name)`` holds a named
    declaration's bars. ``custom[kind]`` holds the records of a ``Custom`` input.
    Lifecycle facts (dividends, splits, delistings, symbol changes) are listed for
    the node's instruments. ``time`` is the platform clock when the callback ran.
    """

    time: datetime
    bars: Mapping[InstrumentId, Bar] = field(default_factory=dict)
    quotes: Mapping[InstrumentId, QuoteBar] = field(default_factory=dict)
    named: Mapping[str, Mapping[InstrumentId, Bar | QuoteBar]] = field(default_factory=dict)
    custom: Mapping[str, tuple] = field(default_factory=dict)
    dividends: tuple[Dividend, ...] = ()
    splits: tuple[Split, ...] = ()
    delistings: tuple[Delisting, ...] = ()
    symbol_changes: tuple[SymbolChange, ...] = ()

    def __post_init__(self):
        aware(self.time)
        object.__setattr__(self, "bars", frozen_map(self.bars))
        object.__setattr__(self, "quotes", frozen_map(self.quotes))
        object.__setattr__(
            self, "named", frozen_map({k: frozen_map(v) for k, v in self.named.items()})
        )
        object.__setattr__(
            self, "custom", frozen_map({k: tuple(v) for k, v in self.custom.items()})
        )
        for name in ("dividends", "splits", "delistings", "symbol_changes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def series(self, name: str) -> Mapping[InstrumentId, Bar | QuoteBar]:
        """Bars of a named declaration, empty when none completed in this slice."""
        return self.named.get(name, frozen_map({}))

    @property
    def empty(self) -> bool:
        """True when the slice carries no record at all."""
        return not (
            self.bars
            or self.quotes
            or any(self.named.values())
            or any(self.custom.values())
            or self.dividends
            or self.splits
            or self.delistings
            or self.symbol_changes
        )

    def instruments(self) -> frozenset[InstrumentId]:
        """Every instrument any record of this slice names."""
        result = set(self.bars) | set(self.quotes)
        for items in self.named.values():
            result.update(items)
        for items in self.custom.values():
            result.update(getattr(item, "instrument", None) for item in items)
        for items in (self.dividends, self.splits, self.delistings, self.symbol_changes):
            result.update(item.instrument for item in items)
        result.discard(None)
        return frozenset(result)


@dataclass(frozen=True)
class Window(Sequence):
    """A rolling window of bars, newest first: ``window[0]`` is the latest bar.

    Adapters may hand a node a platform-backed window instead; this one wraps a
    plain sequence and serves the memory adapter and tests.
    """

    bars: tuple

    def __post_init__(self):
        object.__setattr__(self, "bars", tuple(self.bars))

    def __getitem__(self, index):
        return self.bars[index]

    def __len__(self) -> int:
        return len(self.bars)

    def column(self, name: str, count: int | None = None) -> tuple[float, ...]:
        """A bar field over the newest ``count`` bars, oldest first."""
        bars = self.bars[: len(self.bars) if count is None else count]
        return tuple(getattr(bar, name) for bar in reversed(bars))

    def oldest_first(self, count: int | None = None) -> tuple:
        bars = self.bars[: len(self.bars) if count is None else count]
        return tuple(reversed(bars))
