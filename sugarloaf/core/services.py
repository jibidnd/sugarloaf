"""Lazy service contracts. Adapters guard every read against snapshot invalidation."""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, TypeVar

from sugarloaf.core.errors import CapabilityError, SnapshotError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.primitives import InstrumentId, finite, frozen_map
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    Capability,
    DataRequirement,
    FundamentalRecord,
    FundamentalsRequirement,
    IndicatorRequirement,
    IndicatorSnapshot,
    Instrument,
    Quote,
    QuotesRequirement,
    Selection,
    ShortAvailability,
    ShortAvailabilityRequirement,
    UniverseMembership,
)

if TYPE_CHECKING:
    # Annotation only: the portfolio package imports this module at runtime.
    from sugarloaf.portfolio.constraints import PortfolioConstraints


@dataclass(frozen=True)
class OpenOrder:
    """A platform order snapshot with signed total and cumulative filled quantities."""

    order_id: str
    instrument: InstrumentId
    quantity: float
    filled: float = 0
    status: str = "open"
    intent_id: str = ""

    @property
    def remaining(self) -> float:
        """Return signed unfilled quantity; cancellation requests do not reduce it."""
        return self.quantity - self.filled


@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable authoritative holdings, cash, marks, and open-order snapshot.

    Holdings use instrument quantities; NAV uses account currency. Prices are
    instrument marks and require multiplier/FX conversion for account valuation.
    These are account facts, never holdings inferred from a desired PositionView.
    """

    holdings: Mapping[InstrumentId, float]
    nav: float
    cash: Mapping[str, float]
    prices: Mapping[InstrumentId, float]
    open_orders: tuple[OpenOrder, ...] = ()
    currency: str = "USD"

    def __post_init__(self):
        finite(self.nav, *self.holdings.values(), *self.cash.values(), *self.prices.values())
        for field in ("holdings", "cash", "prices"):
            object.__setattr__(self, field, frozen_map(getattr(self, field)))
        object.__setattr__(self, "open_orders", tuple(self.open_orders))


class MarketData(Protocol):
    """Declaration-guarded market reads at the Context's admitted time boundaries.

    Providers raise CapabilityError for undeclared or unsupported reads,
    NotReady for insufficient/freshness-failing data, and SnapshotError after
    backing state changes.
    """
    def bars(self, requirement: BarsRequirement, instrument: InstrumentId) -> tuple[Bar, ...]:
        """Return declared completed bars in chronological order after causal filtering."""
        ...
    def quotes(
        self, requirement: QuotesRequirement, instrument: InstrumentId
    ) -> tuple[Quote, ...]:
        """Return declared bid/ask observations in chronological order."""
        ...
    def indicator(
        self, requirement: IndicatorRequirement, instrument: InstrumentId
    ) -> IndicatorSnapshot:
        """Return admitted provider-native indicator outputs and initialization provenance."""
        ...
    def ready(self, requirement: DataRequirement) -> tuple[InstrumentId, ...]:
        """Return the selected instruments whose declared read is usable now.

        A node whose requirement sets ``require_all=False`` calls this to learn
        which members it can read, then decides what a partial selection means.
        """
        ...
    def membership(self, name: str) -> UniverseMembership:
        """Return ready membership; an explicit empty universe differs from unavailable data."""
        ...
    def resolve(self, selection: Selection) -> tuple[InstrumentId, ...]:
        """Resolve explicit instruments or ready named membership."""
        ...
    def anchor(self, instrument: InstrumentId, at: datetime, convention: str) -> float:
        """Read an exact declared price anchor; never substitute a nearby observation."""
        ...
    def fundamentals(
        self, requirement: FundamentalsRequirement, instrument: InstrumentId
    ) -> FundamentalRecord:
        """Return the latest declared fundamental record known at graph time."""
        ...
    def short_availability(
        self, requirement: ShortAvailabilityRequirement, instrument: InstrumentId
    ) -> ShortAvailability:
        """Return the latest declared borrow terms known at graph time."""
        ...


class PortfolioState(Protocol):
    """Authoritative account snapshots and explicit buying-power/borrow capabilities."""
    def snapshot(self) -> AccountSnapshot:
        """Return the account snapshot authorized by the node's declarations."""
        ...
    def buying_power(self, instrument: InstrumentId, quantity: float) -> Capability:
        """Check platform permission for a proposed signed order quantity."""
        ...
    def borrow(self, instrument: InstrumentId, quantity: float) -> Capability:
        """Check borrow availability for a positive required short quantity."""
        ...


class InstrumentCatalog(Protocol):
    """Instrument metadata and exchange-session access behind the adapter boundary."""
    def get(self, instrument: InstrumentId) -> Instrument:
        """Return declared instrument metadata, including lot size and multiplier."""
        ...
    def is_open(self, instrument: InstrumentId, at: datetime, session: str) -> bool:
        """Check whether the instrument trades at an aware timestamp in the named session."""
        ...
    def session_close(self, instrument: InstrumentId, after: datetime, count: int = 1) -> datetime:
        """Return the close of the count-th regular session ending strictly after ``after``.

        This is how a horizon is counted in trading sessions rather than elapsed
        calendar time: one session after a Friday close ends at Monday's close.
        """
        ...
    def session_open(self, instrument: InstrumentId, after: datetime, count: int = 1) -> datetime:
        """Return the open of the count-th regular session starting strictly after ``after``.

        The counterpart of ``session_close`` for intervals measured from an open:
        asked at a Friday close, the first session opens Monday morning.
        """
        ...


class CostModel(Protocol):
    """Estimated transaction costs in account currency; estimation does not charge the account."""
    def estimate(self, instrument: InstrumentId, quantity: float, price: float) -> float:
        """Estimate costs for signed quantity and instrument price in account currency."""
        ...


class ValuationModel(Protocol):
    """Executable instrument valuation in account currency."""
    def unit_value(self, instrument: InstrumentId) -> float:
        """Executable mark * multiplier * FX, in account currency."""
        ...


T = TypeVar("T")


class Context:
    """One lazy logical snapshot with explicit graph and availability boundaries.

    Args:
        batch: Admitted sequence, graph cutoff, receipt time, and events.
        factories: Named zero-argument service constructors, called on first access.
        guard: Provider check rejecting reads after backing state changes.
        node_id: Registered reader identity; execution Contexts use an empty ID.
        cache: Optional provider-owned computation cache. Every Context a provider
            creates for one snapshot shares it, so two nodes reading the same bars
            fetch them once. The provider clears it whenever its state changes.
            Without one, the Context keeps a private cache and clears it on close.

    Services and computations are cached within the snapshot. Context does not
    copy the platform data store or become authoritative account state. Do not
    retain it in node state after evaluation; the runtime releases it.
    """

    def __init__(
        self,
        batch: EventBatch,
        factories: Mapping[str, Callable],
        guard: Callable[[], None] = lambda: None,
        node_id: str = "",
        cache: dict | None = None,
    ):
        # Capture snapshot boundaries and lazy constructors rather than copying a
        # market-data store. Provider guards invalidate reads when backing state changes.
        self._batch = batch
        self._factories = frozen_map(factories)
        self._guard = guard
        self._services: dict = {}
        # Remember who owns the cache: close() may clear a private cache, but must
        # not empty a provider cache that sibling Contexts are still reading.
        self._owns_cache = cache is None
        self._cache: dict = {} if cache is None else cache
        self._node_id = node_id
        self._closed = False

    @property
    def batch(self) -> EventBatch:
        """Return the normalized admitted batch underlying this snapshot."""
        return self._batch

    @property
    def now(self) -> datetime:
        """Return this snapshot's cutoff; runtime node snapshots use graph time."""
        # Node evaluation sees the shared observation clock. Execution contexts are
        # created separately by the gateway at current authoritative platform time.
        return self._batch.cutoff

    @property
    def available_at(self) -> datetime:
        """Knowledge boundary, distinct from the shared data-driven graph clock."""
        # Use receipt/knowledge time to reject data not yet available, even if its
        # observation end is at or before the logical graph clock.
        return self._batch.received_at

    @property
    def snapshot_id(self) -> int:
        """Return the admitted batch sequence used for snapshot identity."""
        return self._batch.sequence

    @property
    def node_id(self) -> str:
        """Return the registered reader identity attached to this Context."""
        return self._node_id

    @property
    def events(self):
        """Return admitted neutral events visible during this evaluation."""
        return self._batch.events

    def check(self):
        """Reject reads after release or provider-state mutation.

        Raises:
            SnapshotError: This Context no longer represents a readable snapshot.
        """
        if self._closed:
            raise SnapshotError("Context has been released")
        self._guard()

    def service(self, name: str):
        """Lazily construct and cache one service within this snapshot.

        Raises:
            CapabilityError: The named service is unavailable.
            SnapshotError: The Context has been released or invalidated.
        """
        # Creating a service is lazy, but every access still validates the Context.
        # A previously cached service handle does not bypass provider snapshot guards.
        self.check()
        if name not in self._services:
            if name not in self._factories:
                raise CapabilityError(f"Service unavailable: {name}")
            self._services[name] = self._factories[name]()
        return self._services[name]

    @property
    def market(self) -> MarketData:
        """Return lazy declared market-data reads."""
        return self.service("market")

    @property
    def portfolio(self) -> PortfolioState:
        """Return lazy authoritative account and buying-power services."""
        return self.service("portfolio")

    @property
    def instruments(self) -> InstrumentCatalog:
        """Return lazy declared instrument metadata and calendars."""
        return self.service("instruments")

    @property
    def costs(self) -> CostModel:
        """Return lazy transaction-cost estimates in account currency."""
        return self.service("costs")

    @property
    def valuation(self) -> ValuationModel:
        """Return lazy executable unit values in account currency."""
        return self.service("valuation")

    @property
    def constraints(self) -> "PortfolioConstraints":
        """Return this provider's configured portfolio safety limits."""
        return self.service("constraints")

    def compute(
        self, key: str, inputs: tuple, parameters: tuple, version: str, function: Callable[[], T]
    ) -> T:
        """Compute once per snapshot and cache by explicit hashable identity.

        Args:
            key: Computation name.
            inputs: Hashable identities of the calculation inputs.
            parameters: Hashable calculation settings.
            version: Implementation or model version.
            function: Zero-argument calculation, called only on a cache miss.

        Returns:
            Cached or newly calculated result. This is not persistent model storage.

        Raises:
            SnapshotError: The Context has been released or invalidated.
            TypeError: A cache-key component is not hashable.
        """
        # Cache derived work by explicit input/model identity within this snapshot.
        # Do not use this as persistent fit storage or reuse it across changed provider state.
        self.check()
        cache_key = (self.snapshot_id, key, inputs, parameters, version)
        if cache_key not in self._cache:
            self._cache[cache_key] = function()
        return self._cache[cache_key]

    def for_node(self, node_id: str) -> "Context":
        """Return a node-labeled Context sharing this snapshot's services, cache, and guard."""
        # Share calculations within a snapshot while attaching reader provenance.
        # The new Context's guard also checks that the parent Context is still valid.
        result = Context(self.batch, self._factories, self.check, node_id, self._cache)
        result._services = self._services
        return result

    def close(self):
        """Release this Context, its service instances, and any cache it owns."""
        # Runtime calls this at evaluation cleanup. Stored Context/service handles
        # are no longer safe to read even if the Python object still exists.
        self._closed = True
        self._services.clear()
        # A provider-owned cache outlives this Context; the provider clears it.
        if self._owns_cache:
            self._cache.clear()
