"""Neutral data, instrument, universe and resource declarations."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.types import InstrumentId, aware, finite, frozen_map


@dataclass(frozen=True)
class Capability:
    """An explicit support decision with a reason when an operation is unavailable."""
    # Unsupported features carry an explicit reason. Adapters must not silently
    # substitute a different data source or order/session interpretation.
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
    # Neutral metadata supports valuation/risk; derivative fields are descriptive
    # and do not by themselves imply executable derivative support in an adapter.
    id: InstrumentId
    ticker: str
    asset_class: str = "equity"
    market: str = "USA"
    currency: str = "USD"
    lot_size: float = 1
    multiplier: float = 1
    tradable: bool = True
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
    """Explicit unique instrument identities or one named universe, never both."""
    # A named universe resolves to its current members through Context; it is not
    # a fixed ticker list. An empty explicit selection has service-specific semantics.
    instruments: tuple[InstrumentId, ...] = ()
    universe: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "instruments", tuple(self.instruments))
        if self.instruments and self.universe is not None:
            raise ContractError("Select explicit instruments or one universe")
        if len(set(self.instruments)) != len(self.instruments):
            raise ContractError("Duplicate selected instrument")


@dataclass(frozen=True)
class Schedule:
    """A local wall time or exchange-session anchor, checked on data-clock ticks."""

    # A declaration names a wall time or exchange-session anchor. The provider
    # uses calendars/timezones when graph data ticks cross that time; no node timer runs.
    at: time | None = None
    timezone: str = "UTC"
    calendar: InstrumentId | None = None
    anchor: str | None = None
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
    """Event kind with optional filters, or a schedule checked on data-clock ticks."""

    # These filters request evaluation, not guaranteed new decisions. A node may
    # also activate when upstream output, lifecycle facts or universe membership changes.
    kind: str = "data"
    instrument: InstrumentId | None = None
    interval: timedelta | None = None
    key: str | None = None
    schedule: Schedule | None = None

    def __post_init__(self):
        if self.schedule is not None and self.kind != "scheduled":
            raise ContractError("A schedule belongs to a scheduled trigger")


class Requirement:
    """Marker for a node's declared service requirements."""
    pass


class DataRequirement(Requirement):
    """Marker for declarations that authorize market-data reads."""
    pass


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
    """
    # This declaration both requests resources and authorizes a matching read.
    # A node must use this same request when calling ctx.market.bars.
    selection: Selection
    interval: timedelta = timedelta(days=1)
    lookback: int = 1
    session: str = "regular"
    normalization: str = "adjusted"
    fill_forward: bool = False
    max_age: timedelta = timedelta(days=4)

    def __post_init__(self):
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
        kind: Provider-supported indicator name.
        parameters: Unique scalar settings normalized into immutable sorted pairs.
        window: Required number of valid native output points.
        outputs: Unique requested output names, such as value or upper/lower.
    """

    # Ask the provider for its native indicator and warmup/readiness metadata.
    # This does not authorize a raw-bar read unless a BarsRequirement is declared too.
    source: BarsRequirement
    kind: str
    parameters: tuple[tuple[str, int | float | str], ...] = ()
    window: int = 1
    outputs: tuple[str, ...] = ("value",)

    def __post_init__(self):
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

    @property
    def selection(self):
        return self.source.selection


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
    """Bid/ask history declaration with explicit session, lookback, and freshness."""
    # Quotes are timestamped bid/ask observations, not an OHLC close fallback.
    # The provider rejects unsupported quote subscriptions rather than synthesizing them.
    selection: Selection
    interval: timedelta = timedelta(minutes=1)
    lookback: int = 1
    session: str = "regular"
    max_age: timedelta = timedelta(minutes=5)
    fill_forward: bool = False

    def __post_init__(self):
        if self.interval <= timedelta(0) or self.lookback < 1 or self.max_age < timedelta(0):
            raise ContractError("Invalid quote request")
        if self.session not in {"regular", "extended"}:
            raise ContractError("Unknown trading session")


@dataclass(frozen=True)
class SeriesRequirement(DataRequirement):
    """Named scalar history with an explicit availability convention and freshness."""
    # Availability convention is part of identity: published data and observation
    # time must not be interchangeable inputs in a causal calculation.
    name: str
    lookback: int = 1
    availability: str = "published"
    max_age: timedelta = timedelta(days=4)

    def __post_init__(self):
        if not self.name or self.lookback < 1 or self.max_age < timedelta(0):
            raise ContractError("Invalid scalar series requirement")


@dataclass(frozen=True)
class PortfolioRequirement(Requirement):
    """Declare account-service capabilities and optional instrument selection.

    Empty selection permits the whole account. Restricted snapshots filter
    holdings, marks, and open orders; NAV and cash remain account-wide.
    """
    # A selected snapshot filters holdings, marks and orders, while NAV/cash stay
    # account-wide. Empty selection grants account scope rather than a zero-asset account.
    fields: tuple[str, ...] = ("holdings", "nav", "open_orders")
    selection: Selection = Selection()


@dataclass(frozen=True)
class InstrumentRequirement(Requirement):
    """Declare metadata and calendar access; does not subscribe to price data."""
    # Metadata/session permission does not create a price-data permission.
    # Declare bars/quotes separately if evaluation also needs prices.
    selection: Selection


@dataclass(frozen=True)
class LiquidityUniverse:
    """Point-in-time price/volume filtering followed by descending dollar-volume selection."""
    # Filtering/ranking rules are neutral. The provider supplies candidate rows
    # and owns native refresh callbacks; selection ranks eligible dollar volume.
    name: str
    count: int
    min_price: float = 0
    min_volume: float = 0
    refresh: Trigger = Trigger("universe")
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
    """Historical ETF constituents selected by weights and explicit missing-weight policy."""
    # The ETF is a stable anchor identity needing a provider binding; discovered
    # constituents are bound separately. Historical membership avoids a current-list shortcut.
    name: str
    etf: InstrumentId
    count: int | None = None
    min_weight: float | None = None
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
class UniverseRequirement(Requirement):
    """Declare ownership of a named universe selection source."""
    # This node owns the named selection source; other readers may reference
    # the same name. Owners must agree on its definition to avoid conflicting membership.
    definition: LiquidityUniverse | ETFConstituentsUniverse


@dataclass(frozen=True)
class ResourceUpdate:
    """Complete replacement of node requirements and triggers, effective next batch."""
    # Emit this through NodeOutputs to change all declarations at the next batch.
    # It is a complete replacement, not an incremental subscription patch.
    requirements: tuple[Requirement, ...]
    triggers: tuple[Trigger, ...]


@dataclass(frozen=True)
class Bar:
    """Completed positive-price OHLCV observation with distinct end and availability times."""
    # end is the completed observation boundary; available_at is when it became
    # usable knowledge. Fill-forward observations are explicitly labeled.
    instrument: InstrumentId
    start: datetime
    end: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    fill_forward: bool = False

    def __post_init__(self):
        for t in (self.start, self.end, self.available_at):
            aware(t)
        finite(self.open, self.high, self.low, self.close, self.volume)
        if (
            self.start >= self.end
            or self.available_at < self.end
            or self.volume < 0
            or self.low <= 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
        ):
            raise ContractError("Invalid completed OHLCV bar")


@dataclass(frozen=True)
class Quote:
    """Positive bid/ask observation with ask at least bid and explicit availability."""
    # Bid and ask may determine executable-side valuation; time and availability
    # remain distinct so delayed quotes cannot enter an earlier knowledge snapshot.
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
    # Scalar series also need explicit availability time; historical indexing by
    # observation date alone can accidentally expose future publications.
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
    """Named selection snapshot; ready empty membership differs from unavailable membership."""
    # An empty but ready tuple means the rule selected no assets. Not-ready or
    # missing membership means selection evidence is unavailable, not a zero target.
    name: str
    members: tuple[InstrumentId, ...]
    cutoff: datetime
    ready: bool = True

    def __post_init__(self):
        aware(self.cutoff)
        object.__setattr__(self, "members", tuple(self.members))
