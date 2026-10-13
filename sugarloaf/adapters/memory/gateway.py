"""Controlled order feedback for tests; fills occur only when the caller supplies them."""

from dataclasses import replace

from sugarloaf.core.errors import ContractError
from sugarloaf.core.services import OpenOrder
from sugarloaf.execution.contracts import OrderUpdate
from sugarloaf.market.contracts import Capability


class MemoryGateway:
    """Record order requests and publish only caller-supplied fills and cancellations.

    Args:
        services (MemoryServices): MemoryServices providing authoritative fixture account state.
        on_update (Callable[[OrderUpdate], None] | None): Optional callback receiving signed
            cumulative OrderUpdates.

    This fixture has no price model, automatic matching, latency, or fill simulator.
    """
    def __init__(self, services, on_update=None):
        self.services = services
        # Tests can wire this callback directly to execution feedback. No matching
        # loop or simulated latency runs in the background.
        self.on_update = on_update or (lambda update: None)
        self.intents = {}
        self.orders = {}
        self.cancel_requests = set()

    def capability(self, policy):
        """Accept regular-session orders of any type; the fixture never fills them itself."""
        return Capability(
            policy.session == "regular", "Memory fixture supports regular-session orders"
        )

    def context(self):
        """Return a fresh execution Context from the fixture provider."""
        return self.services.context()

    def _publish_account(self):
        account = self.services.account
        # Publish the authoritative open-order fixture after each order change.
        # Terminal entries stay in the gateway ledger but leave account open_orders.
        self.services.set_account(
            replace(
                account,
                open_orders=tuple(
                    o
                    for o in self.orders.values()
                    if o.status not in {"filled", "canceled", "rejected", "expired"}
                ),
            )
        )

    def submit(self, intent):
        """Record one unique intent, publish an open order, and return its platform ID.

        Raises:
            ContractError: The intent identity was already submitted.
        """
        if intent.intent_id in self.intents:
            raise ContractError("Duplicate intent submission")
        # IDs identify fixture orders; neutral intent IDs independently prevent
        # submitting the same request twice. Submission itself fills nothing.
        order_id = str(len(self.orders) + 1)
        self.intents[intent.intent_id] = intent
        self.orders[order_id] = OpenOrder(
            order_id, intent.instrument, intent.quantity, intent_id=intent.intent_id
        )
        self._publish_account()
        # Notify only after the account exposes the open order. This synchronous
        # feedback deliberately exercises execution's submission bookkeeping.
        self.on_update(
            OrderUpdate(order_id, intent.intent_id, "open", 0, self.services.batch.cutoff)
        )
        return order_id

    def cancel(self, order_id):
        """Request cancellation without releasing pending exposure or changing order status."""
        if order_id not in self.orders:
            raise ContractError("Unknown order")
        # A request alone changes neither filled quantity nor pending exposure.
        # The caller must explicitly confirm it, or supply an intervening fill.
        self.cancel_requests.add(order_id)

    def cancel_status(self, order_id: str) -> Capability | None:
        """Fixture requests are accepted immediately; confirmation remains explicit."""
        if order_id not in self.orders:
            raise ContractError("Unknown order")
        return Capability(True) if order_id in self.cancel_requests else None

    def confirm_cancel(self, order_id):
        """Publish terminal cancellation with any prior fills; fully filled orders stay filled."""
        order = self.orders[order_id]
        # A completed fill wins over a later cancel confirmation. Preserve the
        # terminal fill and its cumulative quantity rather than reopening exposure.
        if order.status == "filled":
            return
        # Confirmation removes the remaining open order but preserves earlier
        # partial fills in both holdings and cumulative feedback.
        self.orders[order_id] = replace(order, status="canceled")
        self._publish_account()
        self.on_update(
            OrderUpdate(
                order_id, order.intent_id, "canceled", order.filled, self.services.batch.cutoff
            )
        )

    def fill(self, order_id, quantity, price=None):
        """Apply an incremental signed fill and publish cumulative feedback.

        Args:
            order_id (str): Existing fixture order identity.
            quantity (float): Signed incremental quantity, including late fills during cancellation.
            price (float | None): Fill price; defaults to the fixture account mark.

        Raises:
            ContractError: Fill sign/size is invalid, or order is filled, rejected, or expired.

        Holdings, cash, and NAV are updated explicitly. This fixture assumes account
        pricing without FX conversion and does not apply market-model fees or slippage.
        """
        order = self.orders[order_id]
        # Cancellation is intentionally not in this rejection set: a late fill
        # during a cancel race can still arrive and must update authoritative state.
        if order.status in {"filled", "rejected", "expired"}:
            raise ContractError("Cannot fill a terminal order")
        # The supplied fill is a signed increment in the original order direction;
        # it cannot exceed the remaining quantity within the numerical tolerance.
        if quantity * order.quantity <= 0 or abs(quantity) > abs(order.remaining) + 1e-10:
            raise ContractError("Invalid incremental fill")
        account = self.services.account
        price = account.prices[order.instrument] if price is None else price
        instrument = self.services.instruments[order.instrument]
        # Apply only the caller's explicit fill. Cash uses its fill price and the
        # instrument multiplier; there is no inferred spread, fee or FX conversion.
        holdings = dict(account.holdings)
        holdings[order.instrument] = holdings.get(order.instrument, 0) + quantity
        cash = dict(account.cash)
        cash[account.currency] = (
            cash.get(account.currency, 0) - quantity * price * instrument.multiplier
        )
        # Mark the resulting holdings at fixture account prices, which may differ
        # from the supplied fill price. All cash is assumed directly comparable.
        nav = sum(cash.values()) + sum(
            q * account.prices[i] * self.services.instruments[i].multiplier
            for i, q in holdings.items()
        )
        # Feedback carries the cumulative total even though fill() accepts a delta.
        # Execution can then identify progress without counting a replay twice.
        filled = order.filled + quantity
        status = "filled" if abs(filled - order.quantity) < 1e-10 else "partial"
        self.orders[order_id] = replace(order, filled=filled, status=status)
        # Update holdings/cash first, then publish open orders, then notify the
        # engine. Its next Context must observe all aspects of the same fixture fill.
        self.services.set_account(replace(account, holdings=holdings, cash=cash, nav=nav))
        self._publish_account()
        self.on_update(
            OrderUpdate(order_id, order.intent_id, status, filled, self.services.batch.cutoff)
        )
