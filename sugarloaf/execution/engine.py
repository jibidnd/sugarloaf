"""Idempotent target reconciliation with authoritative gateway state.

Intent identities are reserved before submit, and submission-time callbacks are
queued until the returned platform ID is bound. A pending cancel continues to
count as possible exposure; replacements wait for a terminal confirmation.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from math import isfinite, trunc

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.services import AccountSnapshot, Context, PortfolioConstraints
from sugarloaf.core.types import InstrumentId
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import (
    TERMINAL_STATUSES,
    ExecutionGateway,
    ExecutionPolicy,
    OrderIntent,
    OrderUpdate,
)

TERMINAL = TERMINAL_STATUSES


@dataclass
class IntentState:
    """Mutable local order reservation and cumulative fills; never authoritative holdings."""
    # This reservation tracks a requested order, including unknown submissions.
    # Actual holdings always come from the fresh gateway account snapshot.
    intent: OrderIntent
    order_id: str | None = None
    status: str = "reserved"
    filled: float = 0.0
    cancel_requested: bool = False


@dataclass
class ExecutionState:
    """Current target, run-long intent ledger, unique sequence, and kill switch."""
    # Keep intent identities for the run to reject duplicate/replayed work. This
    # in-memory ledger is not a persistent restart/recovery mechanism.
    target: PositionView | None = None
    intents: dict[str, IntentState] = field(default_factory=dict)
    sequence: int = 0
    kill_switch: bool = False


class RiskGate:
    """Check an order against fresh account state and adverse pending-fill exposure."""

    def check(
        self,
        intent: OrderIntent,
        target: PositionView,
        ctx: Context,
        policy: ExecutionPolicy,
        state: ExecutionState,
    ) -> None:
        """Reject orders that violate timing, tradability, valuation, or portfolio limits.

        Args:
            intent: Proposed signed whole-lot order.
            target: Active desired position target authorizing this order.
            ctx: Fresh broker/account services for this check.
            policy: Session, lifetime, and target-age rules.
            state: Kill switch and local reservations absent from broker snapshots.

        Raises:
            NotReady: Stale targets, closed sessions, exposure limits, borrow, or
                buying power prevent submission.
            ContractError: Instrument is unsupported or quantity is malformed.

        Possible buys and sells are bounded separately. An order may reduce an
        existing breach only when no breach worsens and no new breach appears.
        """
        # Check the proposed order at execution time, not only when the node decided.
        # Fresh marks, sessions, holdings and outstanding orders can change permission.
        if state.kill_switch:
            raise NotReady("RISK_KILL_SWITCH")
        if (
            ctx.now >= target.metadata.expires_at
            or ctx.now < target.metadata.cutoff
            or ctx.now - target.metadata.cutoff > policy.max_target_age
        ):
            raise NotReady("RISK_STALE_TARGET")
        instrument = ctx.instruments.get(intent.instrument)
        if instrument.asset_class != "equity":
            raise ContractError("Initial execution supports equities only")
        if not instrument.tradable:
            raise NotReady("RISK_NOT_TRADABLE")
        if not ctx.instruments.is_open(intent.instrument, ctx.now, policy.session):
            raise NotReady("RISK_SESSION_CLOSED")
        if not isfinite(intent.quantity) or intent.quantity == 0:
            raise ContractError("Order quantity must be finite and nonzero")
        if (
            abs(
                intent.quantity / instrument.lot_size - round(intent.quantity / instrument.lot_size)
            )
            > 1e-8
        ):
            raise ContractError("Order is not a whole lot")
        account = ctx.portfolio.snapshot()
        if account.nav <= 0:
            raise NotReady("RISK_INVALID_NAV")
        current = {
            i: q * ctx.valuation.unit_value(i) / account.nav
            for i, q in account.holdings.items()
            if q != 0
        }
        # Risk works in account-NAV weights, even when targets/orders use quantities.
        # The range includes current holdings plus adverse outstanding fill scenarios.
        lower, upper = self._pending_bounds(current, account, ctx, state)
        unit_value = ctx.valuation.unit_value(intent.instrument)
        if not isfinite(unit_value) or unit_value <= 0:
            raise NotReady("RISK_INVALID_PRICE")
        before_lower, before_upper = dict(lower), dict(upper)
        delta = intent.quantity * unit_value / account.nav
        lower[intent.instrument] = lower.get(intent.instrument, 0) + min(delta, 0)
        upper[intent.instrument] = upper.get(intent.instrument, 0) + max(delta, 0)
        # Orders can fill in any order. Pending sells do not fund pending buys.
        limits = ctx.constraints
        desired_weights = {
            i: (q if target.units == "weight" else q * ctx.valuation.unit_value(i) / account.nav)
            for i, q in target.positions.items()
        }
        remaining_turnover = sum(abs(w - current.get(i, 0)) for i, w in desired_weights.items())
        # Count turnover already filled for this target as well as its remaining
        # requested change, so splitting a trade cannot evade the turnover limit.
        filled_turnover = sum(
            abs(entry.filled) * ctx.valuation.unit_value(entry.intent.instrument) / account.nav
            for entry in state.intents.values()
            if entry.intent.target_id == target.metadata.view_id
        )
        if remaining_turnover + filled_turnover > limits.turnover_limit + 1e-8:
            raise NotReady("RISK_TURNOVER_LIMIT")
        worst_gross = sum(
            max(abs(lower.get(i, 0)), abs(upper.get(i, 0))) for i in set(lower) | set(upper)
        )
        failures = []
        if worst_gross > min(limits.budget, limits.gross_limit) + 1e-8:
            failures.append("pending-fill gross exposure")
        if sum(lower.values()) < limits.net_min - 1e-8:
            failures.append("pending-fill minimum net exposure")
        if sum(upper.values()) > min(limits.net_max, 1 - limits.cash_reserve) + 1e-8:
            failures.append("pending-fill maximum net exposure/cash reserve")
        if any(w < -1e-8 for w in lower.values()) and not limits.allow_short:
            failures.append("shorting disabled")
        if any(w < limits.min_weight - 1e-8 for w in lower.values() if abs(w) > 1e-8):
            failures.append("minimum position bound")
        if any(w > limits.max_weight + 1e-8 for w in upper.values()):
            failures.append("maximum position bound")
        # An existing breach must not deadlock its remediation (including entering
        # a positive minimum-net allocation from cash). No possible fill ordering
        # may worsen any pre-existing breach or introduce a new one.
        for instrument_id in set(lower) | set(upper):
            before_lower.setdefault(instrument_id, 0)
            before_upper.setdefault(instrument_id, 0)
        non_worsening = all(
            after <= before + 1e-8
            for before, after in zip(
                self._breaches(before_lower, before_upper, limits),
                self._breaches(lower, upper, limits),
            )
        )
        if failures and not non_worsening:
            raise NotReady("RISK_EXPOSURE: " + ", ".join(failures))
        projected_quantity = lower[intent.instrument] * account.nav / unit_value
        if projected_quantity < 0 and intent.quantity < 0:
            borrow = ctx.portfolio.borrow(intent.instrument, abs(projected_quantity))
            if not borrow.supported:
                raise NotReady("RISK_BORROW_DENIED: " + borrow.reason)
        buying_power = ctx.portfolio.buying_power(intent.instrument, intent.quantity)
        if not buying_power.supported:
            raise NotReady("RISK_BUYING_POWER_DENIED: " + buying_power.reason)

    def _pending_bounds(
        self,
        current: dict[InstrumentId, float],
        account: AccountSnapshot,
        ctx: Context,
        state: ExecutionState,
    ) -> tuple[dict[InstrumentId, float], dict[InstrumentId, float]]:
        """Bound exposure if all pending sells fill, or all pending buys fill.

        Local intents absent from the platform snapshot still reserve exposure.
        Matching platform order IDs prevent counting the same order twice.
        """
        # Treat all possible buys and sells as independent adverse fills. A cancel
        # request does not remove an order from these exposure bounds.
        lower, upper = dict(current), dict(current)
        for order in account.open_orders:
            if order.status not in TERMINAL:
                delta = order.remaining * ctx.valuation.unit_value(order.instrument) / account.nav
                lower[order.instrument] = lower.get(order.instrument, 0) + min(delta, 0)
                upper[order.instrument] = upper.get(order.instrument, 0) + max(delta, 0)
        # Match IDs before adding local reservations: a broker-visible order and its
        # local intent describe the same possible exposure and must not be counted twice.
        known_orders = {order.order_id for order in account.open_orders}
        for entry in state.intents.values():
            if entry.status not in TERMINAL and entry.order_id not in known_orders:
                instrument_id = entry.intent.instrument
                delta = (
                    (entry.intent.quantity - entry.filled)
                    * ctx.valuation.unit_value(instrument_id)
                    / account.nav
                )
                lower[instrument_id] = lower.get(instrument_id, 0) + min(delta, 0)
                upper[instrument_id] = upper.get(instrument_id, 0) + max(delta, 0)
        return lower, upper

    @staticmethod
    def _breaches(
        low: dict[InstrumentId, float],
        high: dict[InstrumentId, float],
        limits: PortfolioConstraints,
    ) -> tuple[float, ...]:
        """Violation magnitudes in fixed aggregate-then-instrument order.

        Before/after comparisons require identical instrument sets. Zero remains a
        permitted exit even when a positive minimum position is configured.
        """
        # Compute comparable violation magnitudes rather than one pass/fail flag.
        # Remediation is allowed only if no existing breach grows and none is introduced.
        net_limit = min(limits.net_max, 1 - limits.cash_reserve)
        ids = sorted(set(low) | set(high))
        return (
            max(
                0,
                sum(max(abs(low.get(i, 0)), abs(high.get(i, 0))) for i in ids)
                - min(limits.budget, limits.gross_limit),
            ),
            max(0, limits.net_min - sum(low.values())),
            max(0, sum(high.values()) - net_limit),
            *(max(0, -low.get(i, 0)) if not limits.allow_short else 0 for i in ids),
            *(
                max(0, limits.min_weight - low.get(i, 0)) if abs(low.get(i, 0)) > 1e-8 else 0
                for i in ids
            ),
            *(max(0, high.get(i, 0) - limits.max_weight) for i in ids),
        )


class ExecutionEngine:
    """Reconcile desired positions against broker holdings and pending orders.

    Args:
        gateway: Platform boundary supplying fresh account state and order operations.
        policy: Order and staleness rules; defaults to regular-session day market orders.
        risk: Optional gate; defaults to timing and worst-case pending-exposure checks.

    Intents supplement broker state rather than replacing holdings. Hosts serialize
    callbacks and drain feedback before reconciliation. State is in-memory only.
    """

    def __init__(
        self,
        gateway: ExecutionGateway,
        policy: ExecutionPolicy | None = None,
        risk: RiskGate | None = None,
    ):
        # Ask the gateway to accept the full policy up front so unsupported order types
        # cannot silently fall back to a different broker behavior.
        self.gateway = gateway
        self.policy = policy or ExecutionPolicy()
        self.risk = risk or RiskGate()
        self.gateway.capability(self.policy).require()
        self.state = ExecutionState()
        self.diagnostics: list[dict] = []
        self.diagnostic_count = 0
        self._busy = False
        self._updates: list[OrderUpdate] = []

    def _record(self, code: str, **fields):
        # Bound diagnostic payload retention while keeping a total count. This is
        # inspection data, separate from the run-long intent reservations.
        self.diagnostic_count += 1
        self.diagnostics.append({"code": code, **fields})
        del self.diagnostics[:-1000]

    def accept(self, target: PositionView) -> None:
        """Replace the target, request superseded cancellations, and reconcile.

        Args:
            target: Immutable desired positions; a new identity supersedes old intents.

        Raises:
            ContractError: An active target identity is reused with different contents.

        Cancellation and submission failures propagate while exposure stays reserved.
        """
        # An immutable target ID may be replayed unchanged, but cannot be edited.
        # A different ID changes order authority and requests cancellation of old work.
        old = self.state.target
        if old is not None and old.metadata.view_id == target.metadata.view_id:
            if old != target:
                raise ContractError("Target identity reused for different content")
        else:
            self.state.target = target
            self._cancel_pending()
        self.reconcile()

    def invalidate(self) -> None:
        """Withdraw the target and request cancellations; retain filled holdings."""
        # Revoking target authority requests outstanding cancellations only. Filled
        # positions remain owned until another explicit target requests an exit.
        self.state.target = None
        self._cancel_pending()

    def _cancel_pending(self):
        # Use a snapshot of the ledger because gateway callbacks can arrive during
        # cancellation; terminal intents are filtered by _request_cancel.
        for entry in tuple(self.state.intents.values()):
            self._request_cancel(entry)

    def _request_cancel(self, entry: IntentState) -> None:
        """Retain exposure, retry failed requests, and await terminal confirmation."""
        # A successful request means the broker accepted the request, not that the order
        # is closed. Reservations wait for terminal order feedback.
        if entry.status in TERMINAL or entry.order_id is None:
            return
        if entry.cancel_requested:
            # Unknown or accepted request outcomes cannot release exposure or
            # authorize another cancellation request.
            outcome = self.gateway.cancel_status(entry.order_id)
            if outcome is None or outcome.supported:
                return
            entry.cancel_requested = False
            self._record(
                "CANCEL_REJECTED", intent_id=entry.intent.intent_id, reason=outcome.reason
            )
        entry.cancel_requested = True
        try:
            self.gateway.cancel(entry.order_id)
        except Exception as error:
            entry.cancel_requested = False
            self._record("CANCEL_FAILED", intent_id=entry.intent.intent_id, reason=str(error))
            raise

    def on_order_update(self, update: OrderUpdate) -> None:
        """Apply signed cumulative fills without initiating reconciliation.

        Args:
            update: Platform status and total filled quantity, rather than fill delta.

        Raises:
            ContractError: Order identity, sign, total, or filled status is inconsistent.

        Feedback during submission is queued until the platform ID is bound. Hosts
        must drain all feedback before reconciling so holdings are not counted twice.
        """
        # The platform may call back before submit returns its order ID. Queue updates
        # during reconciliation so identity binding precedes cumulative fill accounting.
        if self._busy:
            self._updates.append(update)
            return
        self._apply_update(update)

    def _apply_update(self, update: OrderUpdate):
        # External orders remain represented by broker snapshots rather than invented
        # local intents. Known intents require matching IDs, fill sign and fill size.
        entry = self.state.intents.get(update.intent_id)
        if entry is None:
            self._record("EXTERNAL_ORDER_UPDATE", order_id=update.order_id)
            return
        if entry.order_id is not None and entry.order_id != update.order_id:
            raise ContractError("Order feedback identity mismatch")
        fill_tolerance = min(1e-8, abs(entry.intent.quantity) * 1e-8)
        if abs(update.filled) > abs(entry.intent.quantity) + fill_tolerance:
            raise ContractError("Cumulative fills exceed submitted quantity")
        if update.filled and update.filled * entry.intent.quantity < 0:
            raise ContractError("Fill has the wrong sign")
        fully_filled = abs(update.filled - entry.intent.quantity) <= fill_tolerance
        if update.status == "filled" and not fully_filled:
            raise ContractError("Filled status requires the complete submitted quantity")
        if abs(update.filled) < abs(entry.filled):
            return  # Delayed or replayed status must not roll back cumulative fills.
        entry.order_id = update.order_id
        # Store the signed cumulative total; adding it to previous fills would double
        # count repeated callbacks and replayed statuses.
        entry.filled = update.filled
        # A delayed open/partial callback can carry the ticket's newer cumulative
        # total. Complete fills remain terminal even if its event status lags.
        if fully_filled:
            entry.status = "filled"
        elif entry.status not in TERMINAL or update.status in TERMINAL:
            entry.status = update.status
        self._record(
            "ORDER_UPDATE",
            intent_id=update.intent_id,
            target_id=entry.intent.target_id,
            order_id=update.order_id,
            status=update.status,
            filled=update.filled,
        )

    def reconcile(self) -> None:
        """Submit desired-minus-held-minus-pending quantities using fresh state.

        Desired quantities are truncated to whole lots. Sells submit first, but their
        cash is not assumed available before confirmed fills. Cancel-requested and
        uncertain orders reserve exposure until authoritative feedback resolves them.

        Submission exceptions propagate with an unknown local reservation; blindly
        retrying could duplicate an order. Calls while already reconciling are ignored.
        """
        # Guard against reentrant submission loops, then apply all callbacks queued
        # during submit after the platform IDs are bound.
        if self._busy:
            return
        self._busy = True
        try:
            # Release every gateway snapshot on success, early return or failure.
            # A custom risk gate must not retain readable execution services afterward.
            with ExitStack() as contexts:
                self._reconcile(contexts)
        finally:
            self._busy = False
            updates, self._updates = self._updates, []
            for update in updates:
                self._apply_update(update)

    def _reconcile(self, contexts: ExitStack):
        # Every pass starts from fresh broker state, not a retained node Context.
        # A target authorizes desired positions; reconciliation computes only the gap.
        ctx = self.gateway.context()
        contexts.callback(ctx.close)
        target = self.state.target
        if target is None or self.state.kill_switch:
            self._cancel_pending()
            return
        if (
            ctx.now >= target.metadata.expires_at
            or ctx.now - target.metadata.cutoff > self.policy.max_target_age
        ):
            self.invalidate()
            self._record("TARGET_EXPIRED", target_id=target.metadata.view_id)
            return
        for entry in tuple(self.state.intents.values()):
            if (
                entry.cancel_requested
                or entry.intent.target_id != target.metadata.view_id
                or ctx.now >= entry.intent.expires_at
            ):
                self._request_cancel(entry)
        if any(
            e.cancel_requested and e.status not in TERMINAL for e in self.state.intents.values()
        ):
            return
        account = ctx.portfolio.snapshot()
        if account.nav <= 0:
            self._record("RISK_INVALID_NAV")
            return
        desired = {}
        for instrument, amount in target.positions.items():
            info = ctx.instruments.get(instrument)
            value = ctx.valuation.unit_value(instrument)
            if value <= 0 or not isfinite(value):
                raise NotReady("Invalid valuation")
            # Convert account weights to instrument quantities using executable unit value,
            # then truncate to whole lots. Quantity targets stay fixed between decisions.
            quantity = amount if target.units == "quantity" else amount * account.nav / value
            desired[instrument] = trunc(quantity / info.lot_size) * info.lot_size
        # Submit sells first; only confirmed fills release exposure/cash.
        # Every submission re-reads current state rather than assuming prior fills.
        ordered = sorted(desired, key=lambda i: (desired[i] - account.holdings.get(i, 0), i.value))
        for instrument in ordered:
            fresh = self.gateway.context()
            contexts.callback(fresh.close)
            actual = fresh.portfolio.snapshot()
            info = fresh.instruments.get(instrument)
            latest_value = fresh.valuation.unit_value(instrument)
            amount = target.positions[instrument]
            latest_quantity = (
                amount if target.units == "quantity" else amount * actual.nav / latest_value
            )
            desired[instrument] = trunc(latest_quantity / info.lot_size) * info.lot_size
            pending = sum(
                o.remaining
                for o in actual.open_orders
                if o.instrument == instrument and o.status not in TERMINAL
            )
            known_orders = {o.order_id for o in actual.open_orders}
            pending += sum(
                e.intent.quantity - e.filled
                for e in self.state.intents.values()
                if e.intent.instrument == instrument
                and e.status not in TERMINAL
                and e.order_id not in known_orders
            )
            # Only the residual gap becomes a new order. Pending quantities include orders
            # that might still fill during cancellation and uncertain local submissions.
            quantity = desired[instrument] - actual.holdings.get(instrument, 0) - pending
            if abs(quantity) < 1e-10:
                continue
            if (
                desired[instrument] != 0
                and abs(quantity * latest_value) < self.policy.min_trade_value
            ):
                continue
            # Rejection cannot create a retry storm for identical target identities.
            if any(
                e.intent.target_id == target.metadata.view_id
                and e.intent.instrument == instrument
                and e.status == "rejected"
                for e in self.state.intents.values()
            ):
                continue
            self.state.sequence += 1
            identity = f"{target.metadata.view_id}:intent:{self.state.sequence}"
            intent = OrderIntent(
                identity,
                target.metadata.view_id,
                instrument,
                quantity,
                fresh.now,
                fresh.now + self.policy.lifetime,
                self.policy.order_type,
                self.policy.time_in_force,
            )
            try:
                self.risk.check(intent, target, fresh, self.policy, self.state)
            except NotReady as error:
                self._record(
                    "RISK_REJECTED",
                    target_id=target.metadata.view_id,
                    instrument=instrument.value,
                    reason=str(error),
                )
                continue
            entry = IntentState(intent)
            # Reserve before the gateway call. If the response is lost, the order may exist
            # remotely; retaining unknown exposure prevents a blind duplicate submission.
            self.state.intents[identity] = entry
            try:
                # The gateway translates this neutral intent into a native asynchronous order.
                # The returned ID is a submission identity, not evidence of a completed fill.
                entry.order_id = self.gateway.submit(intent)
                entry.status = "submitted"
            except Exception as error:
                # Submission outcome may be unknown: reserve exposure until authoritative
                # feedback resolves it. Blind retries could duplicate a filled order.
                entry.status = "unknown"
                self._record("SUBMISSION_UNKNOWN", intent_id=identity, reason=str(error))
                raise
            self._record(
                "ORDER_SUBMITTED",
                target_id=target.metadata.view_id,
                intent_id=identity,
                order_id=entry.order_id,
            )
