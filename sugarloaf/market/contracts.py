"""Neutral data, instrument, universe and resource declarations."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.primitives import InstrumentId, aware, finite, frozen_map


@dataclass(frozen=True)
class Capability:
    """An explicit support decision with a reason when an operation is unavailable.

    Adapters report unsupported features this way instead of silently substituting
    a different data source or order/session interpretation.
    """

    # `reason` explains a refusal to a human reading a diagnostic; it is unused
    # when supported is True.
    supported: bool
    reason: str = ""

    def require(self) -> None:
        """Raise CapabilityError when the declared operation is unsupported."""
        if not self.supported:
            raise CapabilityError(self.reason or "Capability unavailable")


@dataclass(frozen=True)
class Instrument:
    """Neutral instrument metadata used for valuation and execution checks.

    Lot size and multiplier must be positive. Optional derivative fields describe
    metadata only; they do not establish adapter or execution support.
    """

    # `id` is the stable identity used everywhere in the graph; `ticker` is only
    # the current display name and may change (for example FB -> META).
    id: InstrumentId
    ticker: str
    asset_class: str = "equity"
    market: str = "USA"
    currency: str = "USD"
    # Orders are placed in whole multiples of lot_size. One unit is worth
    # price * multiplier in the instrument's currency: 1 for a share, 50 for an
    # E-mini S&P 500 future.
    lot_size: float = 1
    multiplier: float = 1
    # False after a delisting or halt: execution refuses to open or change a position.
    tradable: bool = True
    # The remaining fields describe derivatives. They are carried as information
    # only; no adapter trades derivatives yet.
    underlying: InstrumentId | None = None
    strike: float | None = None
    expiry: datetime | None = None
    exercise_style: str | None = None
    settlement: str | None = None

    def __post_init__(self):
        finite(self.lot_size, self.multiplier)
        if self.lot_size <= 0 or self.multiplier <= 0:
            raise ContractError("Lot size and multiplier must be positive")
        if self.expiry is not None:
            aware(self.expiry)


@dataclass(frozen=True)
class Selection:
    """Explicit unique instrument identities or one named universe, never both.

    A named universe resolves to its current members through Context; it is not a
    fixed ticker list. An empty explicit selection has service-specific semantics.
    """

    # Use exactly one of the two. `universe` is the name a UniverseRequirement
    # defined, for example Selection(universe="liquid-500").
    instruments: tuple[InstrumentId, ...] = ()
    universe: str | None = None

    def __post_init__(self):
        # Accept any iterable from the caller but store an immutable tuple.
        object.__setattr__(self, "instruments", tuple(self.instruments))
        if self.instruments and self.universe is not None:
            raise ContractError("Select explicit instruments or one universe")
        if len(set(self.instruments)) != len(self.instruments):
            raise ContractError("Duplicate selected instrument")


@dataclass(frozen=True)
class Schedule:
    """A local wall time or exchange-session anchor, checked on data-clock ticks.

    The provider resolves calendars and timezones when graph data ticks cross the
    declared time; no per-node timer runs.

    Give a wall-time schedule a ``calendar`` unless you really mean every
    calendar day. Without one, ``Schedule(at=time(10, 0))`` also occurs on
    weekends and holidays. No data arrives then, and a missed occurrence fires at
    the next data tick, so the node runs at Monday's 09:31 open as well as at
    Monday's 10:00. With ``calendar=instrument`` the schedule occurs only on that
    instrument's trading sessions. (Seen in a native parity run: a "10:00 daily"
    entry traded at 09:31 after a weekend and after a market closure.)
    """

    # Two ways to say "when", of which exactly one is used:
    #   Schedule(at=time(10, 30), timezone="America/New_York")   a wall-clock time
    #   Schedule(calendar=spy, anchor="open", offset=timedelta(minutes=1))
    #       one minute after that instrument's exchange opens, on trading days only
    at: time | None = None
    timezone: str = "UTC"
    # The instrument whose exchange calendar defines sessions and holidays.
    calendar: InstrumentId | None = None
    anchor: str | None = None
    # Added to the wall time or session anchor; may be negative ("before the close").
    offset: timedelta = timedelta(0)

    def __post_init__(self):
        if (self.at is None) == (self.anchor is None):
            raise ContractError("Choose exactly one schedule wall time or session anchor")
        if self.anchor not in {None, "open", "close"}:
            raise ContractError("Unknown session anchor")
        if self.anchor and self.calendar is None:
            raise ContractError("Session schedules require an instrument calendar")
        if not self.timezone or (self.at is not None and self.at.tzinfo is not None):
            raise ContractError("Schedule requires a named timezone and naive local time")


@dataclass(frozen=True)
class Trigger:
    """Event kind with optional filters, or a schedule checked on data-clock ticks.

    A trigger requests evaluation; it does not guarantee a new decision. A node may
    also activate when upstream output, lifecycle facts or universe membership changes.
    """

    # Examples:
    #   Trigger()                                  any batch that carries data
    #   Trigger("bar_close", spy, timedelta(minutes=5))   each completed 5-minute SPY bar
    #   Trigger("scheduled", schedule=Schedule(...))      a crossed wall/session time
    # A filter left as None matches anything.
    kind: str = "data"
    instrument: InstrumentId | None = None
    interval: timedelta | None = None
    key: str | None = None
    schedule: Schedule | None = None

    def __post_init__(self):
        if self.schedule is not None and self.kind != "scheduled":
            raise ContractError("A schedule belongs to a scheduled trigger")

    def matches(self, event) -> bool:
        """Whether an Event has this kind and passes every filter that is set.

        Scheduled triggers are not matched here: the provider checks whether their
        occurrence was crossed between two graph times.
        """
        return (
            self.kind == event.kind
            and (self.instrument is None or self.instrument == event.instrument)
            and (self.interval is None or self.interval == event.interval)
            and (self.key is None or self.key == event.key)
        )


class Requirement:
    """Marker for a node's declared service requirements."""


class DataRequirement(Requirement):
    """Marker for declarations that authorize market-data reads."""


@dataclass(frozen=True)
class BarsRequirement(DataRequirement):
    """Completed-bar subscription and read contract.

    Attributes:
        selection: Explicit instruments or named universe.
        interval: Positive bar duration supported by the provider.
        lookback: Minimum number of usable observations.
        session: Regular or extended trading session.
        normalization: Raw, adjusted, split_adjusted, or total_return pricing.
        fill_forward: Permit provider-generated fill-forward bars.
        max_age: Maximum age relative to graph time after causal filtering.
        require_all: Withdraw the node unless every selected instrument is ready.
            False lets evaluation proceed with a partial selection; the node then
            calls ``ctx.market.ready(requirement)`` and decides what that means.

    The declaration both requests resources and authorizes a matching read: a node
    passes this same object to ``ctx.market.bars``.
    """

    selection: Selection
    interval: timedelta = timedelta(days=1)
    lookback: int = 1
    session: str = "regular"
    # "adjusted" prices are corrected for splits and dividends, "split_adjusted"
    # for splits only, "total_return" reinvests dividends, "raw" is as traded.
    # How and when a vendor applies those corrections is the adapter's concern:
    # it rebuilds what it must and reports a DataReset (see core/events.py).
    normalization: str = "adjusted"
    fill_forward: bool = False
    # The default of four days lets a daily bar from Friday stay usable over a
    # long weekend; an intraday strategy should set something much shorter.
    max_age: timedelta = timedelta(days=4)
    require_all: bool = True

    def __post_init__(self):
        # Reject impossible settings here, before any provider subscribes to data.
        if self.interval <= timedelta(0) or self.lookback < 1 or self.max_age < timedelta(0):
            raise ContractError("Invalid bar interval, lookback, or freshness")
        if self.session not in {"regular", "extended"}:
            raise ContractError("Unknown trading session")
        if self.normalization not in {"raw", "adjusted", "split_adjusted", "total_return"}:
            raise ContractError("Unknown normalization")


@dataclass(frozen=True)
class IndicatorRequirement(DataRequirement):
    """Provider-native indicator declaration; no portable formula fallback.

    Attributes:
        source: Backing bar declaration with lookback=1; grants no raw-bar read.
            Its ``require_all`` also governs this indicator's readiness.
        kind: Provider-supported indicator name.
        parameters: Unique scalar settings normalized into immutable sorted pairs.
        window: Required number of valid native output points.
        outputs: Unique requested output names, such as value or upper/lower.
    """

    # Example: a 20-bar EMA of 5-minute SPY bars, keeping the last two values:
    #   IndicatorRequirement(bars, "ema", (("period", 20),), window=2)
    source: BarsRequirement
    kind: str
    parameters: tuple[tuple[str, int | float | str], ...] = ()
    window: int = 1
    outputs: tuple[str, ...] = ("value",)

    def __post_init__(self):
        # Sorting makes two requirements with the same settings compare equal
        # whatever order the caller listed them in, so they share one indicator.
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        if not self.kind or self.window < 1 or self.source.lookback != 1:
            raise ContractError("Indicator needs kind, positive window and source lookback=1")
        if len(dict(self.parameters)) != len(self.parameters):
            raise ContractError("Duplicate indicator parameter")
        if not self.outputs or len(set(self.outputs)) != len(self.outputs):
            raise ContractError("Indicator output names must be unique and nonempty")
        for _, value in self.parameters:
            if isinstance(value, (int, float)):
                finite(value)
            elif not isinstance(value, str):
                raise ContractError("Indicator parameters must be scalar")

    # An indicator covers the same instruments, with the same readiness rule, as
    # the bars it is computed from. Exposing both lets shared code treat bar,
    # quote and indicator requirements alike.
    @property
    def selection(self) -> Selection:
        return self.source.selection

    @property
    def require_all(self) -> bool:
        return self.source.require_all


@dataclass(frozen=True)
class IndicatorPoint:
    """Timestamped finite named outputs from one admitted native indicator update."""

    time: datetime
    values: Mapping[str, float]

    def __post_init__(self):
        aware(self.time)
        finite(*self.values.values())
        object.__setattr__(self, "values", frozen_map(self.values))


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Chronological indicator points, native readiness, and initialization provenance."""

    points: tuple[IndicatorPoint, ...]
    ready: bool
    provenance: Mapping[str, object]

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "provenance", frozen_map(self.provenance))


@dataclass(frozen=True)
class QuotesRequirement(DataRequirement):
    """Bid/ask history declaration with explicit session, lookback, and freshness.

    Quotes are timestamped bid/ask observations, never an OHLC close fallback; a
    provider rejects quote subscriptions it cannot supply. ``require_all`` has the
    same meaning as on BarsRequirement.
    """

    selection: Selection
    interval: timedelta = timedelta(minutes=1)
    lookback: int = 1
    session: str = "regular"
    max_age: timedelta = timedelta(minutes=5)
    fill_forward: bool = False
    require_all: bool = True

    def __post_init__(self):
        if self.interval <= timedelta(0) or self.lookback < 1 or self.max_age < timedelta(0):
            raise ContractError("Invalid quote request")
        if self.session not in {"regular", "extended"}:
            raise ContractError("Unknown trading session")


@dataclass(frozen=True)
class PortfolioRequirement(Requirement):
    """Declare account-service access and an optional instrument selection.

    Empty selection permits the whole account, not a zero-asset account. A
    restricted snapshot filters holdings, marks, and open orders; NAV and cash
    remain account-wide.
    """

    selection: Selection = Selection()


@dataclass(frozen=True)
class InstrumentRequirement(Requirement):
    """Declare metadata and calendar access; does not subscribe to price data."""

    selection: Selection


@dataclass(frozen=True)
class LiquidityUniverse:
    """Point-in-time price/volume filtering followed by descending dollar-volume selection.

    The rule is neutral; the provider supplies candidate rows and owns native
    refresh callbacks.
    """

    # Example: the 500 most liquid stocks priced at USD5 or more:
    #   LiquidityUniverse("liquid-500", 500, min_price=5)
    name: str
    count: int
    min_price: float = 0
    min_volume: float = 0
    # Keep only securities the provider has company fundamental data for. In
    # practice this drops funds and notes, which otherwise rank among the most
    # liquid "stocks". The filter is applied before the count is taken.
    require_fundamentals: bool = False
    # Membership is recomputed only when this trigger fires.
    refresh: Trigger = Trigger("universe")
    # How long a member's data resources are kept after it drops out, so a brief
    # exit and re-entry does not discard and re-warm its history.
    minimum_residence: timedelta = timedelta(0)
    source: str = "us_equity_liquidity"

    def __post_init__(self):
        finite(self.min_price, self.min_volume)
        if (
            not self.name
            or self.count < 0
            or min(self.min_price, self.min_volume) < 0
            or self.minimum_residence < timedelta(0)
        ):
            raise ContractError("Invalid liquidity selection")


@dataclass(frozen=True)
class ETFConstituentsUniverse:
    """Historical ETF constituents selected by weights and explicit missing-weight policy.

    The ETF is a stable anchor identity needing a provider binding; discovered
    constituents are bound separately.
    """

    name: str
    etf: InstrumentId
    # Keep the `count` largest weights, and/or those of at least `min_weight`.
    count: int | None = None
    min_weight: float | None = None
    # What to do with a constituent whose weight the data source omits:
    # "exclude" it, "include" it (ranked last), or "error" (membership not ready).
    missing_weight: str = "exclude"
    refresh: Trigger = Trigger("universe")
    minimum_residence: timedelta = timedelta(0)
    source: str = "historical_etf_constituents"

    def __post_init__(self):
        if (
            not self.name
            or (self.count is not None and self.count < 0)
            or self.minimum_residence < timedelta(0)
        ):
            raise ContractError("Invalid ETF selection")
        if self.min_weight is not None:
            finite(self.min_weight)
            if self.min_weight < 0:
                raise ContractError("Minimum constituent weight must be nonnegative")
        if self.missing_weight not in {"exclude", "include", "error"}:
            raise ContractError("Unknown missing-weight policy")


@dataclass(frozen=True)
class FundamentalRecord:
    """A security's company fundamentals as known at ``time``.

    ``time`` is when the provider read the record: it is both the as-of and the knowledge
    time. Fields the provider does not have are None. ``price`` is the record's traded price,
    not a price adjusted for later corporate actions. ``cik`` is the issuer's SEC Central
    Index Key, stored as text without leading zeros.
    """

    instrument: InstrumentId
    time: datetime
    cik: str | None = None
    market_cap: float | None = None
    price: float | None = None
    dollar_volume: float | None = None

    def __post_init__(self):
        aware(self.time)
        finite(*(x for x in (self.market_cap, self.price, self.dollar_volume) if x is not None))
        # One form for every provider: EDGAR's indexes give CIKs unpadded, its JSON APIs
        # padded to ten digits.
        if self.cik is not None:
            cik = str(self.cik).strip().lstrip("0")
            if cik and not cik.isdigit():
                raise ContractError("A CIK is a number")
            object.__setattr__(self, "cik", cik or None)


@dataclass(frozen=True)
class FundamentalUniverse:
    """Members chosen by a rule over the provider's daily fundamental records.

    ``rule(at, records)`` receives the selection time and one FundamentalRecord per
    security, and returns the instruments to hold; any it returns that are not among the
    records are ignored. The rule must use only what is known at ``at``: the records, and
    any source the strategy reads point-in-time. A rule that cannot decide (its source does
    not cover ``at``) raises NotReady, which leaves the universe unready.

    Definitions compare by name, version and settings, not by the rule object, so a node may
    rebuild its rule without rebuilding the native source. Change ``version`` when the rule's
    meaning changes.
    """

    # Example: stocks of issuers that filed with the SEC in the last 30 days, where
    # filers(at) is the strategy's own point-in-time source of CIKs:
    #   FundamentalUniverse("recent-filers",
    #                       lambda at, rows: [r.instrument for r in rows if r.cik in filers(at)])
    name: str
    rule: Callable[[datetime, tuple[FundamentalRecord, ...]], Iterable[InstrumentId]] = field(
        compare=False
    )
    version: str = "1"
    refresh: Trigger = Trigger("universe")
    minimum_residence: timedelta = timedelta(0)
    source: str = "us_equity_fundamental"

    def __post_init__(self):
        if not self.name or not callable(self.rule) or self.minimum_residence < timedelta(0):
            raise ContractError("Invalid fundamental selection")


@dataclass(frozen=True)
class UniverseRequirement(Requirement):
    """Declare ownership of a named universe selection source.

    Other nodes may read the same name. Every owner must give the same definition.
    """

    definition: LiquidityUniverse | ETFConstituentsUniverse | FundamentalUniverse


@dataclass(frozen=True)
class FundamentalsRequirement(DataRequirement):
    """Read authorization for the selected securities' latest fundamental records.

    Attributes:
        selection: Explicit instruments or named universe.
        max_age: Oldest record accepted, relative to graph time.
        require_all: Withdraw the node unless every selected security has a record.
    """

    selection: Selection
    max_age: timedelta = timedelta(days=4)
    require_all: bool = False

    def __post_init__(self):
        if self.max_age < timedelta(0):
            raise ContractError("Invalid fundamentals freshness")


@dataclass(frozen=True)
class ShortAvailability:
    """Borrow terms for shorting a security, as reported at ``time``.

    ``fee_rate`` is the annual borrow fee as a decimal and ``shortable`` the quantity the
    provider reports as available; None means the provider reports no limit for that field.
    A provider without real borrow data returns no record at all, so that a reader cannot
    mistake missing data for free, unlimited borrow.
    """

    instrument: InstrumentId
    time: datetime
    fee_rate: float | None = None
    shortable: float | None = None

    def __post_init__(self):
        aware(self.time)
        finite(*(x for x in (self.fee_rate, self.shortable) if x is not None))


@dataclass(frozen=True)
class ShortAvailabilityRequirement(DataRequirement):
    """Read authorization for the selected securities' borrow terms."""

    selection: Selection
    require_all: bool = False


@dataclass(frozen=True)
class ResourceUpdate:
    """Complete replacement of node requirements and triggers, effective next batch.

    Emitted through NodeOutputs. It replaces every declaration; it is not an
    incremental subscription patch.
    """

    requirements: tuple[Requirement, ...]
    triggers: tuple[Trigger, ...]


@dataclass(frozen=True)
class Bar:
    """Completed OHLCV observation with distinct end and availability times.

    ``end`` is the completed observation boundary; ``available_at`` is when it
    became usable knowledge. Prices may be zero or negative (some futures and
    back-adjusted series are), so a consumer that divides by a price checks its
    sign itself.
    """

    instrument: InstrumentId
    start: datetime
    end: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    # True when the provider repeated the previous bar because nothing traded.
    fill_forward: bool = False

    def __post_init__(self):
        for t in (self.start, self.end, self.available_at):
            aware(t)
        finite(self.open, self.high, self.low, self.close, self.volume)
        # A bar cannot be known before it ends, and its range must contain its
        # open and close. Price sign is deliberately not checked (see docstring).
        if (
            self.start >= self.end
            or self.available_at < self.end
            or self.volume < 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
        ):
            raise ContractError("Invalid completed OHLCV bar")


@dataclass(frozen=True)
class Quote:
    """Positive bid/ask observation with ask at least bid and explicit availability."""

    # `time` is when the quote was observed and `available_at` when it became
    # known, so a delayed quote cannot enter an earlier decision.
    instrument: InstrumentId
    time: datetime
    available_at: datetime
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None

    def __post_init__(self):
        aware(self.time)
        aware(self.available_at)
        finite(self.bid, self.ask)
        if self.bid <= 0 or self.ask < self.bid or self.available_at < self.time:
            raise ContractError("Invalid bid/ask quote")


@dataclass(frozen=True)
class Observation:
    """Finite scalar value with aware observation and availability timestamps."""

    # Used for exact price anchors in outcome evaluation. Indexing history by
    # observation date alone can expose a value before it was published, so the
    # availability time is carried explicitly.
    time: datetime
    available_at: datetime
    value: float

    def __post_init__(self):
        aware(self.time)
        aware(self.available_at)
        finite(self.value)
        if self.available_at < self.time:
            raise ContractError("Observation cannot be available before observation time")


@dataclass(frozen=True)
class UniverseMembership:
    """Named selection snapshot; ready empty membership differs from unavailable membership.

    An empty but ready tuple means the rule selected no assets. Not-ready or
    missing membership means selection evidence is unavailable, not a zero target.
    """

    name: str
    members: tuple[InstrumentId, ...]
    # The time the selection was made from; a node may not read membership whose
    # cutoff is later than its own decision time.
    cutoff: datetime
    ready: bool = True

    def __post_init__(self):
        aware(self.cutoff)
        object.__setattr__(self, "members", tuple(self.members))
