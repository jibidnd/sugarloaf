"""Lazy service contracts. Adapters guard every read against snapshot invalidation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol, TypeVar

from sugarloaf.core.events import EventBatch
from sugarloaf.core.types import InstrumentId, finite, frozen_map
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    Capability,
    IndicatorRequirement,
    IndicatorSnapshot,
    Instrument,
    Observation,
    Quote,
    QuotesRequirement,
    Selection,
    SeriesRequirement,
    UniverseMembership,
)


@dataclass(frozen=True)
class OpenOrder:
    """A platform order snapshot with signed total and cumulative filled quantities."""
    # Signed quantity minus signed cumulative filled is the remaining possible
    # exposure; a cancellation request does not erase that reservation.
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
    """
    # These are authoritative account totals, not inferred holdings from node
    # outputs. A PositionView expresses what is desired, not what has filled.
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
    def series(self, requirement: SeriesRequirement) -> tuple[Observation, ...]:
        """Return declared scalar history under its availability convention."""
        ...
    def indicator(
        self, requirement: IndicatorRequirement, instrument: InstrumentId
    ) -> IndicatorSnapshot:
        """Return admitted provider-native indicator outputs and initialization provenance."""
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


class PortfolioState(Protocol):
    """Authoritative account snapshots and explicit buying-power/borrow capabilities."""
    def snapshot(self) -> AccountSnapshot:
        """Return the account snapshot authorized by the node's declarations."""
        ...
    def buying_power(self, instrument: InstrumentId, quantity: float) -> Capability:
        """Check platform permission for a proposed signed order quantity."""
        ...
    def borrow(self, instrument: InstrumentId, quantity: float) -> Capability:
        """Check confirmed borrow availability for a positive required short quantity."""
        ...


class InstrumentCatalog(Protocol):
    """Instrument metadata and exchange-session access behind the adapter boundary."""
    def get(self, instrument: InstrumentId) -> Instrument:
        """Return declared instrument metadata, including lot size and multiplier."""
        ...
    def is_open(self, instrument: InstrumentId, at: datetime, session: str) -> bool:
        """Check whether the instrument trades at an aware timestamp in the named session."""
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


@dataclass(frozen=True)
class PortfolioConstraints:
    """Exposure limits expressed as fractions of account NAV.

    Gross exposure is bounded by both budget and gross_limit. Net exposure also
    respects cash_reserve. Turnover is the sum of absolute weight changes.
    A zero position remains an allowed exit despite a positive min_weight.
    """
    # These fractions use the whole account NAV. Branch budgets and model risk
    # settings may differ, but execution applies its own account-level safety limits.
    budget: float = 1.0
    allow_short: bool = False
    min_weight: float = 0.0
    max_weight: float = 1.0
    gross_limit: float = 1.0
    net_min: float = 0.0
    net_max: float = 1.0
    cash_reserve: float = 0.0
    turnover_limit: float = 2.0

    def __post_init__(self):
        from sugarloaf.core.errors import ContractError
        from sugarloaf.core.types import finite

        finite(*vars(self).values())
        if (
            self.budget < 0
            or self.gross_limit < 0
            or self.turnover_limit < 0
            or self.min_weight > self.max_weight
            or self.net_min > self.net_max
            or not 0 <= self.cash_reserve <= 1
        ):
            raise ContractError("Invalid portfolio constraints")


T = TypeVar("T")


class Context:
    """One lazy logical snapshot with explicit graph and availability boundaries.

    Args:
        batch: Admitted sequence, graph cutoff, receipt time, and events.
        factories: Named zero-argument service constructors, called on first access.
        guard: Provider check rejecting reads after backing state changes.
        node_id: Registered reader identity; execution Contexts use an empty ID.

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
    ):
        # Capture snapshot boundaries and lazy constructors rather than copying a
        # market-data store. Provider guards invalidate reads when backing state changes.
        self._batch = batch
        self._factories = frozen_map(factories)
        self._guard = guard
        self._services: dict = {}
        self._cache: dict = {}
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
        from sugarloaf.core.errors import SnapshotError

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
        from sugarloaf.core.errors import CapabilityError

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
    def constraints(self) -> PortfolioConstraints:
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
        result = Context(self.batch, self._factories, self.check, node_id)
        result._services = self._services
        result._cache = self._cache
        return result

    def close(self):
        """Release this Context and clear its shared cache and service instances."""
        # Runtime calls this at evaluation cleanup. Stored Context/service handles
        # are no longer safe to read even if the Python object still exists.
        self._closed = True
        self._cache.clear()
        self._services.clear()
