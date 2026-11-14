"""Execution settings, account records and the order-sizing rule shared by adapters.

A position target is turned into orders by the adapter with the platform's own
tools; what is shared here is the policy (order type, thresholds), the neutral
account picture an adapter reports, and the one sizing rule that decides whether
a difference between target and holdings is worth an order.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, trunc

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, aware, finite, frozen_map

ORDER_TYPES = frozenset({"market", "market_on_open", "market_on_close"})
TIMES_IN_FORCE = frozenset({"day", "gtc"})
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired", "invalid"})
ORDER_STATUSES = TERMINAL_STATUSES | {"submitted", "partial", "pending_cancel", "new"}


@dataclass(frozen=True)
class ExecutionPolicy:
    """How a position target becomes orders.

    Attributes:
        order_type: ``market``, ``market_on_open`` or ``market_on_close``; the
            platform decides when such an order may be sent and how it fills.
        time_in_force: ``gtc`` or ``day``, applied by the platform.
        min_trade_value: Smallest order, in account currency, worth sending.
        rebalance_band: Smallest difference, as a fraction of NAV, worth an order
            for a weight target. A weight target's desired quantity moves with
            every mark; without a band every batch would re-trade that drift.
        cancel_on_new_target: Cancel this runner's open orders in an instrument
            before sizing it against a new target.

    An explicit exit to zero is never suppressed by a threshold.
    """

    order_type: str = "market"
    time_in_force: str = "gtc"
    min_trade_value: float = 0.0
    rebalance_band: float = 0.0025
    cancel_on_new_target: bool = True

    def __post_init__(self):
        if self.order_type not in ORDER_TYPES:
            raise ContractError("Unknown order type")
        if self.time_in_force not in TIMES_IN_FORCE:
            raise ContractError("Unknown time in force")
        finite(self.min_trade_value, self.rebalance_band)
        if self.min_trade_value < 0 or not 0 <= self.rebalance_band < 1:
            raise ContractError("Invalid execution thresholds")


@dataclass(frozen=True)
class OpenOrder:
    """An order the platform still holds; ``filled`` is the signed cumulative fill."""

    order_id: str
    instrument: InstrumentId
    quantity: float
    filled: float = 0.0
    tag: str = ""

    @property
    def remaining(self) -> float:
        return self.quantity - self.filled


@dataclass(frozen=True)
class Portfolio:
    """The account as the platform reports it when a node or the runner asks.

    Holdings are signed instrument quantities, prices the platform's current marks,
    NAV and cash in account currency.
    """

    time: datetime
    nav: float
    cash: float
    holdings: Mapping[InstrumentId, float]
    prices: Mapping[InstrumentId, float]
    open_orders: tuple[OpenOrder, ...] = ()
    currency: str = "USD"

    def __post_init__(self):
        aware(self.time)
        finite(self.nav, self.cash, *self.holdings.values(), *self.prices.values())
        object.__setattr__(self, "holdings", frozen_map(self.holdings))
        object.__setattr__(self, "prices", frozen_map(self.prices))
        object.__setattr__(self, "open_orders", tuple(self.open_orders))

    def weight(self, instrument: InstrumentId) -> float:
        """The holding's fraction of NAV at the current mark, zero without a mark."""
        quantity = self.holdings.get(instrument, 0.0)
        price = self.prices.get(instrument)
        if not quantity or price is None or self.nav <= 0:
            return 0.0
        return quantity * price / self.nav

    def weights(self) -> dict[InstrumentId, float]:
        return {i: self.weight(i) for i, q in self.holdings.items() if q}

    def pending(self, instrument: InstrumentId) -> float:
        """Signed unfilled quantity of the platform's open orders in the instrument."""
        return sum(o.remaining for o in self.open_orders if o.instrument == instrument)


@dataclass(frozen=True)
class OrderRecord:
    """What the adapter did for one instrument of one target."""

    target_id: str
    instrument: InstrumentId
    quantity: float
    order_id: str | None = None
    status: str = "submitted"
    reason: str = ""


@dataclass(frozen=True)
class OrderUpdate:
    """Platform order feedback: status and signed cumulative fill, never a delta."""

    order_id: str
    instrument: InstrumentId
    status: str
    filled: float
    time: datetime
    tag: str = ""
    message: str = ""

    def __post_init__(self):
        finite(self.filled)
        aware(self.time)
        if self.status not in ORDER_STATUSES:
            raise ContractError(f"Unknown order status: {self.status}")


def desired_quantity(
    amount: float, units: str, price: float, nav: float, lot_size: float = 1.0
) -> tuple[float, float]:
    """The target's quantity in whole lots and exactly, from a weight or a quantity.

    A 0.2 weight with NAV 10,010 and a 100.50 price wants exactly 19.92 units,
    which is 19 whole units. Both numbers are returned because the rebalance band
    compares the exact one and an order sends the whole-lot one.

    Raises:
        ContractError: The price or NAV is not positive, or the lot size is invalid.
    """
    finite(amount, price, nav, lot_size)
    if price <= 0 or nav <= 0 or lot_size <= 0:
        raise ContractError("Sizing needs a positive price, NAV and lot size")
    exact = amount if units == "quantity" else amount * nav / price
    return trunc(exact / lot_size) * lot_size, exact


def order_quantity(
    whole: float,
    exact: float,
    held: float,
    pending: float,
    price: float,
    nav: float,
    units: str,
    policy: ExecutionPolicy,
) -> float:
    """The signed quantity to order now, or zero when no order is worth sending.

    Args:
        whole: Desired quantity in whole lots.
        exact: Desired quantity before rounding.
        held: Current holding.
        pending: Signed unfilled quantity already on the platform.
        price: Current mark.
        nav: Account NAV.
        units: ``weight`` or ``quantity``; only weight targets are banded.
        policy: Thresholds.

    The residual gap is desired minus held minus pending. An exit to zero is always
    sent. Otherwise the order is skipped when it is worth less than
    ``min_trade_value``, or, for a weight target, when the exact gap is inside the
    rebalance band or a whole-lot order would leave the account no closer.
    """
    quantity = whole - held - pending
    if abs(quantity) < 1e-10 or not isfinite(quantity):
        return 0.0
    if whole == 0 and exact == 0:
        return quantity
    if abs(quantity * price) < policy.min_trade_value:
        return 0.0
    if units == "weight":
        gap_now = abs(exact - held - pending)
        gap_after = abs(exact - whole)
        if gap_now * price < policy.rebalance_band * nav or gap_after >= gap_now:
            return 0.0
    return quantity
