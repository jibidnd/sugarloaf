"""Execution interfaces use fresh adapter state, not a retained decision Context."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, aware, finite
from sugarloaf.core.services import Context
from sugarloaf.market.contracts import Capability

# Only terminal statuses release an order's remaining reservation. An open,
# partial or pending-cancel order can still change holdings.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})
# Every status a gateway may report. Anything else is a contract error, so a new
# brokerage state is noticed instead of being treated as "still open".
ORDER_UPDATE_STATUSES = TERMINAL_STATUSES | {"open", "submitted", "partial", "pending_cancel"}


@dataclass(frozen=True)
class ExecutionPolicy:
    """Order/session settings and staleness limits, separate from portfolio risk.

    Attributes:
        order_type, session, time_in_force: Native order settings. An adapter
            rejects unsupported combinations before trading.
        lifetime: How long one order intent may stay open before cancellation.
        max_target_age: Maximum elapsed calendar time since the target's cutoff.
            It is a backstop: the target's own expiry is the precise control. The
            default spans a long weekend so a Friday-close decision can trade Monday.
        min_trade_value: Smallest order, in account currency, worth submitting.
        rebalance_band: Smallest order, as a fraction of NAV, worth submitting for
            a weight target. A weight target's desired quantity moves with every
            mark; without a band the engine would re-trade that drift on every
            batch. For the same reason a weight target never sends a whole-lot
            order that would leave the account no closer to its exact target.
            Quantity targets are exact instructions and are not banded.

    Neither threshold ever suppresses an explicit exit to zero. Order lifetime and
    target age limit different things and neither implies a timed exit.
    """

    order_type: str = "market"
    session: str = "regular"
    time_in_force: str = "day"
    lifetime: timedelta = timedelta(minutes=5)
    max_target_age: timedelta = timedelta(days=4)
    min_trade_value: float = 0.0
    # 0.0025 is 0.25% of NAV: USD250 on a USD100,000 account. Set 0 to trade
    # every whole-lot difference, or raise it to trade less often.
    rebalance_band: float = 0.0025

    def __post_init__(self):
        # NaN would make the comparisons below False and silently disable a threshold.
        finite(self.min_trade_value, self.rebalance_band)
        if self.min_trade_value < 0:
            raise ContractError("Minimum trade value must be nonnegative in account currency")
        if not 0 <= self.rebalance_band < 1:
            raise ContractError("Rebalance band must be a NAV fraction in [0, 1)")
        if self.lifetime <= timedelta(0) or self.max_target_age <= timedelta(0):
            raise ContractError("Order lifetime and maximum target age must be positive")
        if self.session not in {"regular", "extended"}:
            raise ContractError("Unknown execution session")


@dataclass(frozen=True)
class OrderIntent:
    """Signed quantity request with a stable local identity reserved before submit.

    Positive quantity buys; negative sells. ``intent_id`` is the local identity;
    the gateway later returns the native order ID.
    """

    intent_id: str
    # The PositionView that authorized this order. When a newer target arrives,
    # the engine cancels intents that still carry an older target_id.
    target_id: str
    instrument: InstrumentId
    quantity: float
    created_at: datetime
    # After this time the engine requests cancellation of whatever is unfilled.
    expires_at: datetime
    order_type: str = "market"
    time_in_force: str = "day"


@dataclass(frozen=True)
class OrderUpdate:
    """Platform feedback; ``filled`` is signed cumulative quantity, not a delta.

    Status must belong to ``ORDER_UPDATE_STATUSES``. Repeat the cumulative total
    unchanged when a status callback repeats. Empty intent identity permits
    feedback for external orders. The engine additionally checks cumulative fills
    against the original intent.
    """

    # Example: a 100-share buy that fills 40 then 60 reports filled=40 with status
    # "partial", then filled=100 with status "filled". It never reports 60.
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
        """Return fresh authoritative account, instrument, and valuation services.

        Each call returns a new snapshot; the engine never reuses the Context
        from the earlier strategy decision.
        """
        ...

    def submit(self, intent: OrderIntent) -> str:
        """Submit one signed intent and return its platform order ID.

        The ID confirms submission, not a fill; fills arrive as OrderUpdates.
        """
        ...

    def cancel(self, order_id: str) -> None:
        """Request cancellation; raise on failure and await terminal order feedback.

        A request can race with a fill, so remaining exposure stays reserved.
        """
        ...

    def cancel_status(self, order_id: str) -> Capability | None:
        """Return the latest cancellation-request outcome, not the order's state.

        None means unknown or unprocessed; supported means accepted; unsupported
        means rejected. An accepted request still reserves pending exposure.
        """
        ...
