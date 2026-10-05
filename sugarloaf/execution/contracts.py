"""Execution interfaces use fresh adapter state, not a retained decision Context."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sugarloaf.core.errors import ContractError
from sugarloaf.core.services import Context
from sugarloaf.core.types import InstrumentId, aware, finite
from sugarloaf.market.contracts import Capability

# Only terminal statuses release an order's remaining reservation. An open,
# partial or pending-cancel order can still change holdings.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})
ORDER_UPDATE_STATUSES = TERMINAL_STATUSES | {
    "open", "submitted", "partial", "pending_cancel"
}


@dataclass(frozen=True)
class ExecutionPolicy:
    """Order/session settings and staleness limits, separate from portfolio risk.

    ``min_trade_value`` is account currency and never suppresses an explicit exit.
    An adapter must reject unsupported order/session combinations before trading.
    """

    # Order lifetime and maximum target age are separate limits: one applies to
    # an intention, the other to the decision authorizing it. Neither implies a timed exit.
    order_type: str = "market"
    session: str = "regular"
    time_in_force: str = "day"
    lifetime: timedelta = timedelta(minutes=5)
    max_target_age: timedelta = timedelta(days=1)
    min_trade_value: float = 0.0

    def __post_init__(self):
        finite(self.min_trade_value)
        if self.min_trade_value < 0:
            raise ContractError("Minimum trade value must be nonnegative in account currency")
        if self.lifetime <= timedelta(0) or self.max_target_age <= timedelta(0):
            raise ContractError("Order lifetime and maximum target age must be positive")
        if self.session not in {"regular", "extended"}:
            raise ContractError("Unknown execution session")


@dataclass(frozen=True)
class OrderIntent:
    """Signed quantity request with a stable local identity reserved before submit."""

    # Positive quantity buys; negative quantity sells. intent_id is local identity
    # reserved before submission, whereas the gateway later returns the native order ID.
    intent_id: str
    target_id: str
    instrument: InstrumentId
    quantity: float
    created_at: datetime
    expires_at: datetime
    order_type: str = "market"
    time_in_force: str = "day"
    limit_price: float | None = None


@dataclass(frozen=True)
class OrderUpdate:
    """Platform feedback; ``filled`` is signed cumulative quantity, not a delta.

    Status must belong to ``ORDER_UPDATE_STATUSES``. Empty intent identity permits
    external order feedback.
    The engine additionally checks cumulative fills against the original intent.
    """

    # Repeat a cumulative total unchanged when a status callback repeats; do not
    # sum callback deltas here. Empty intent_id permits unrelated external broker orders.
    order_id: str
    intent_id: str
    status: str
    filled: float
    time: datetime
    message: str = ""

    def __post_init__(self):
        finite(self.filled)
        aware(self.time)
        if not self.order_id:
            raise ContractError("Order feedback requires a platform order identity")
        if self.status not in ORDER_UPDATE_STATUSES:
            raise ContractError(f"Unknown order feedback status: {self.status}")
        if self.status in {"filled", "partial"} and self.filled == 0:
            raise ContractError("A fill status requires nonzero cumulative fills")


class ExecutionGateway(Protocol):
    """Platform order boundary. Each context exposes fresh authoritative state.

    Submit returns a platform order ID. Cancel requests cancellation; the order
    remains possible exposure until terminal feedback confirms its disposition.
    A rejected cancel request raises; it does not reject the original order.

    ``cancel_status`` reports only the latest cancellation request outcome: None
    means unprocessed/unknown, supported means accepted, unsupported means rejected.
    The engine polls outstanding requests and can retry a confirmed rejection.
    """

    def capability(self, policy: ExecutionPolicy) -> Capability:
        """Return whether the platform supports the complete execution policy."""
        ...
    def context(self) -> Context:
        """Return fresh authoritative account, instrument, and valuation services."""
        # An implementation returns a new authoritative execution snapshot each time.
        # The engine must not reuse the Context from the earlier strategy decision.
        ...
    def submit(self, intent: OrderIntent) -> str:
        """Submit one signed intent and return its platform order ID."""
        # Returning a native ID confirms submission identity, not fill completion.
        # The platform reports actual execution through subsequent OrderUpdates.
        ...
    def cancel(self, order_id: str) -> None:
        """Request cancellation; raise on failure and await terminal order feedback."""
        # A request can race with a fill. Keep possible remaining exposure reserved
        # until a terminal update confirms the order's disposition.
        ...
    def cancel_status(self, order_id: str) -> Capability | None:
        """Return the latest cancellation-request outcome, not the order's state.

        None means unknown or unprocessed; supported means accepted; unsupported
        means rejected. An accepted request still reserves pending exposure.
        """
        ...
