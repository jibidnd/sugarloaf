"""Idempotent target reconciliation with authoritative gateway state.

Intent identities are reserved before submit, and submission-time callbacks are
queued until the returned platform ID is bound. A pending cancel continues to
count as possible exposure; replacements wait for a terminal confirmation.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from math import isfinite, trunc

from sugarloaf.core.errors import ContractError, NotReady, OrderRefused
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.services import AccountSnapshot, Context
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import (
    TERMINAL_STATUSES,
    ExecutionGateway,
    ExecutionPolicy,
    OrderIntent,
    OrderUpdate,
)
from sugarloaf.portfolio.constraints import TOLERANCE, breach_name, breaches


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
    """Current target, live intent ledger, and the run-unique intent sequence.

    ``intents`` holds every intent of the current target plus any older intent
    that has not yet reached a terminal status. Finished intents of superseded
    targets are dropped, so the ledger stays small over a long run instead of
    growing with every order ever sent.
    """
    # This in-memory ledger is not a persistent restart/recovery mechanism.
    target: PositionView | None = None
    intents: dict[str, IntentState] = field(default_factory=dict)
    # Never reset: it keeps intent identities unique for the whole run even after
    # older intents have been dropped from the ledger.
    sequence: int = 0


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
            policy: Target-age rule.
            state: Local reservations that are absent from broker snapshots.

        Raises:
            NotReady: Stale targets, exposure limits, borrow, or buying power
                prevent submission.
            ContractError: Instrument is unsupported or quantity is malformed.

        Possible buys and sells are bounded separately. An order may reduce an
        existing breach only when no breach worsens and no new breach appears.
        """
        # Check the proposed order at execution time, not only when the node decided.
        # Fresh marks, holdings and outstanding orders can change permission.
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
        # Whether the session is open is not checked here. When an order may be
        # sent, and what happens to one sent outside the session, is the
        # platform's decision: it may queue the order for the next open, convert
        # it, or refuse it, and a refusal comes back as OrderRefused.
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
        limits = ctx.constraints
        # Measure every limit before adding this order, then again after. Comparing
        # the two is what lets a reducing order through while a breach still exists.
        before = breaches(lower, upper, limits)
        delta = intent.quantity * unit_value / account.nav
        # Orders can fill in any order. Pending sells do not fund pending buys:
        # a buy only raises the upper bound and a sell only lowers the lower bound.
        lower[intent.instrument] = lower.get(intent.instrument, 0) + min(delta, 0)
        upper[intent.instrument] = upper.get(intent.instrument, 0) + max(delta, 0)
        desired_weights = {}
        for i, q in target.positions.items():
            try:
                desired_weights[i] = (
                    q
                    if target.units == "weight"
                    else q * ctx.valuation.unit_value(i) / account.nav
                )
            except NotReady:
                # This instrument has no mark, so the engine cannot order it yet and
                # it adds no turnover now. Leaving it out lets the other instruments
                # trade. When it gains a price, its own order passes through this
                # same check with everything filled so far already counted.
                continue
        remaining_turnover = sum(abs(w - current.get(i, 0)) for i, w in desired_weights.items())
        # Count turnover already filled for this target as well as its remaining
        # requested change, so splitting a trade cannot evade the turnover limit.
        filled_turnover = sum(
            abs(entry.filled) * ctx.valuation.unit_value(entry.intent.instrument) / account.nav
            for entry in state.intents.values()
            if entry.intent.target_id == target.metadata.view_id
        )
        if remaining_turnover + filled_turnover > limits.turnover_limit + TOLERANCE:
            raise NotReady("RISK_TURNOVER_LIMIT")
        # An existing breach must not deadlock its remediation (including entering
        # a positive minimum-net allocation from cash). So the rule is not "no
        # breach may exist" but "this order may not make any breach larger".
        # A limit absent from `before` was satisfied there, hence the default of 0.
        after = breaches(lower, upper, limits)
        worsened = sorted(
            {
                breach_name(key)
                for key, excess in after.items()
                if excess > before.get(key, 0) + TOLERANCE
            }
        )
        if worsened:
            raise NotReady("RISK_EXPOSURE: " + ", ".join(worsened))
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
            if order.status not in TERMINAL_STATUSES:
                delta = order.remaining * ctx.valuation.unit_value(order.instrument) / account.nav
                lower[order.instrument] = lower.get(order.instrument, 0) + min(delta, 0)
                upper[order.instrument] = upper.get(order.instrument, 0) + max(delta, 0)
        # Match IDs before adding local reservations: a broker-visible order and its
        # local intent describe the same possible exposure and must not be counted twice.
        known_orders = {order.order_id for order in account.open_orders}
        for entry in state.intents.values():
            if entry.status not in TERMINAL_STATUSES and entry.order_id not in known_orders:
                instrument_id = entry.intent.instrument
                delta = (
                    (entry.intent.quantity - entry.filled)
                    * ctx.valuation.unit_value(instrument_id)
                    / account.nav
                )
                lower[instrument_id] = lower.get(instrument_id, 0) + min(delta, 0)
                upper[instrument_id] = upper.get(instrument_id, 0) + max(delta, 0)
        return lower, upper


class ExecutionEngine:
    """Reconcile desired positions against broker holdings and pending orders.

    Args:
        gateway: Platform boundary supplying fresh account state and order operations.
        policy: Order and staleness rules; defaults to regular-session market orders,
            good till cancelled.
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
            self._forget_finished()
        self.reconcile()

    def cancel_orders(self, instrument: InstrumentId) -> None:
        """Request cancellation of this engine's unfilled orders in one instrument.

        The target stays. Used when the orders' quantities no longer mean what they
        did, as after a split; the next reconciliation sizes replacements once the
        cancellations are confirmed.
        """
        for entry in tuple(self.state.intents.values()):
            if entry.intent.instrument == instrument:
                self._request_cancel(entry)

    def invalidate(self) -> None:
        """Withdraw the target and request cancellations; retain filled holdings."""
        # Revoking target authority requests outstanding cancellations only. Filled
        # positions remain owned until another explicit target requests an exit.
        self.state.target = None
        self._cancel_pending()
        self._forget_finished()

    def _superseded(self, entry: IntentState) -> bool:
        """Whether an intent is finished and belongs to a target that is no longer current."""
        target = self.state.target
        current = target.metadata.view_id if target is not None else None
        return entry.status in TERMINAL_STATUSES and entry.intent.target_id != current

    def _forget_finished(self) -> None:
        """Drop finished intents of superseded targets so the ledger stays bounded.

        A finished order can no longer change holdings, and only the current
        target's intents feed its turnover and rejection checks. Open orders of an
        older target stay in the ledger until terminal feedback arrives, because
        they still reserve exposure. Without this, a graph that issues a target
        every bar would grow the ledger for the whole run and every reconciliation
        would scan all of it.
        """
        self.state.intents = {
            identity: entry
            for identity, entry in self.state.intents.items()
            if not self._superseded(entry)
        }

    def _cancel_pending(self):
        # Use a snapshot of the ledger because gateway callbacks can arrive during
        # cancellation; terminal intents are filtered by _request_cancel.
        for entry in tuple(self.state.intents.values()):
            self._request_cancel(entry)

    def _request_cancel(self, entry: IntentState) -> None:
        """Retain exposure, retry failed requests, and await terminal confirmation."""
        # A successful request means the broker accepted the request, not that the order
        # is closed. Reservations wait for terminal order feedback.
        if entry.status in TERMINAL_STATUSES or entry.order_id is None:
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

        Feedback that arrives during a reconciliation is queued until the order
        being submitted has its platform ID, and is applied before broker state is
        next read. A host passes feedback on as it arrives; it must not hold it
        back, or holdings and this ledger would describe different moments.
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
        elif entry.status not in TERMINAL_STATUSES or update.status in TERMINAL_STATUSES:
            entry.status = update.status
        self._record(
            "ORDER_UPDATE",
            intent_id=update.intent_id,
            target_id=entry.intent.target_id,
            order_id=update.order_id,
            status=update.status,
            filled=update.filled,
        )
        # An old target's order has now finished: it reserves nothing further.
        # Later feedback for it is harmless and is recorded as an external update;
        # holdings always come from the broker snapshot, never from this ledger.
        if self._superseded(entry):
            del self.state.intents[update.intent_id]

    def reconcile(self) -> None:
        """Submit desired-minus-held-minus-pending quantities using fresh state.

        Desired quantities are truncated to whole lots. Sells submit first, but their
        cash is not assumed available before confirmed fills. Cancel-requested and
        uncertain orders reserve exposure until authoritative feedback resolves them.

        An order is skipped when it is smaller than ``min_trade_value`` or, for a
        weight target, smaller than ``rebalance_band`` of NAV. An exit to zero is
        never skipped. An instrument without a usable mark is skipped with a
        VALUATION_UNAVAILABLE diagnostic; the others still trade.

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
            self._apply_queued()

    def _apply_queued(self) -> None:
        """Apply feedback queued while an order was being submitted."""
        updates, self._updates = self._updates, []
        for update in updates:
            self._apply_update(update)

    def _desired(
        self,
        target: PositionView,
        instrument: InstrumentId,
        ctx: Context,
        account: AccountSnapshot,
    ) -> tuple[float, float, float]:
        """Return the desired quantity in whole lots, the same before rounding, and unit value.

        For example, a 0.2 weight with NAV USD10,010 and a USD100.50 unit value
        wants exactly 19.92 units, which is 19 whole units. A quantity target is
        already in units and is only truncated to whole lots.

        Both numbers are returned because they answer different questions. The
        whole-lot number is what can actually be ordered. The exact number tells
        how far the account really is from its target, which is what the
        rebalance band must compare: judged in whole lots, that example would look
        a full share (1% of NAV) off target after a 0.5% price move.

        Raises:
            NotReady: The instrument has no positive finite mark, or NAV is not
                positive. The caller records this and skips the instrument.
        """
        if account.nav <= 0:
            raise NotReady("RISK_INVALID_NAV")
        info = ctx.instruments.get(instrument)
        # unit_value itself raises NotReady when the account has no price for the
        # instrument, for example a universe member that has not traded yet.
        value = ctx.valuation.unit_value(instrument)
        if not isfinite(value) or value <= 0:
            raise NotReady("No valid instrument valuation")
        amount = target.positions[instrument]
        quantity = amount if target.units == "quantity" else amount * account.nav / value
        # Truncate toward zero so rounding never asks for more than the target.
        return trunc(quantity / info.lot_size) * info.lot_size, quantity, value

    def _reconcile(self, contexts: ExitStack):
        target = self.state.target
        if target is None:
            # Without a target there is nothing to buy or sell: only make sure old
            # orders are being cancelled. No broker snapshot is needed for that,
            # which keeps the many batches between decisions cheap.
            self._cancel_pending()
            return
        # Every pass starts from fresh broker state, not a retained node Context.
        # A target authorizes desired positions; reconciliation computes only the gap.
        ctx = self.gateway.context()
        contexts.callback(ctx.close)
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
            e.cancel_requested and e.status not in TERMINAL_STATUSES
            for e in self.state.intents.values()
        ):
            # A cancellation is still in flight. Its order might yet fill, so wait
            # for terminal feedback before sizing any replacement order.
            return
        account = ctx.portfolio.snapshot()
        if account.nav <= 0:
            self._record("RISK_INVALID_NAV")
            return
        # First pass: find each instrument's gap, only to decide submission order.
        # An instrument that cannot be valued is recorded and left out; one missing
        # price must not stop the rest of the portfolio from trading.
        gaps = {}
        for instrument in target.positions:
            try:
                desired, _, _ = self._desired(target, instrument, ctx, account)
            except NotReady as error:
                self._record(
                    "VALUATION_UNAVAILABLE",
                    target_id=target.metadata.view_id,
                    instrument=instrument.value,
                    reason=str(error),
                )
                continue
            gaps[instrument] = desired - account.holdings.get(instrument, 0)
        # Submit sells first (most negative gap first); only confirmed fills release
        # exposure/cash. The instrument name breaks ties so the order is deterministic.
        for instrument in sorted(gaps, key=lambda i: (gaps[i], i.value)):
            # Second pass: re-read broker state before every submission. An earlier
            # order in this loop may already have filled and changed NAV or holdings.
            #
            # First bring the ledger to the same moment. Sending an order can make
            # the platform fill other, older orders before it returns. Their fills
            # are then in the holdings read below; if their feedback stayed queued
            # they would also still count as pending, and the next instruments
            # would be traded against a position counted twice. (Seen natively: on
            # daily bars all of yesterday's orders fill in one instant, and one
            # drift order sent in that instant produced 37 reversing orders.) The
            # previous submission has returned, so every queued update now refers
            # to an order whose platform ID is bound.
            self._apply_queued()
            fresh = self.gateway.context()
            contexts.callback(fresh.close)
            actual = fresh.portfolio.snapshot()
            try:
                desired, exact, value = self._desired(target, instrument, fresh, actual)
            except NotReady as error:
                self._record(
                    "VALUATION_UNAVAILABLE",
                    target_id=target.metadata.view_id,
                    instrument=instrument.value,
                    reason=str(error),
                )
                continue
            pending = sum(
                o.remaining
                for o in actual.open_orders
                if o.instrument == instrument and o.status not in TERMINAL_STATUSES
            )
            known_orders = {o.order_id for o in actual.open_orders}
            pending += sum(
                e.intent.quantity - e.filled
                for e in self.state.intents.values()
                if e.intent.instrument == instrument
                and e.status not in TERMINAL_STATUSES
                and e.order_id not in known_orders
            )
            # Only the residual gap becomes a new order. Pending quantities include orders
            # that might still fill during cancellation and uncertain local submissions.
            quantity = desired - actual.holdings.get(instrument, 0) - pending
            if abs(quantity) < 1e-10:
                continue
            # Skip orders too small to be worth sending. An explicit exit (desired
            # zero) is exempt from both thresholds: a strategy must always be able
            # to close a position completely.
            if desired != 0:
                if abs(quantity * value) < self.policy.min_trade_value:
                    continue
                # A weight target's desired quantity moves with every price tick.
                # Without a band, a 20% target would buy or sell a share or two on
                # nearly every batch just to chase that drift. A quantity target is
                # an exact instruction, so it is always completed.
                #
                # The drift is measured from the exact (unrounded) desired quantity.
                # Held 20, exactly 19.92 wanted: the account is 0.08 units off, well
                # inside the band, even though whole lots would say "sell one".
                if target.units == "weight":
                    # How far the account is from the exact target now, in units,
                    # and how far it would be once this whole-lot order filled.
                    gap_now = abs(exact - actual.holdings.get(instrument, 0) - pending)
                    gap_after = abs(exact - desired)
                    if gap_now * value < self.policy.rebalance_band * actual.nav:
                        continue
                    # Never send an order that leaves the account no closer to its
                    # target. Orders are whole lots, so one can overshoot: held 50
                    # with exactly 49.55 wanted is 0.45 off, and selling one share
                    # would leave it 0.55 off on the other side. This can only
                    # happen when one lot is worth more than the band.
                    #
                    # It does not stop every single-lot order in such an account.
                    # When the exact target crosses a whole lot (49.95 wanted, 51
                    # held) the one-lot order is a real improvement and is sent.
                    # A native two-week run of a USD100,000 account in SPY, where
                    # one share is 0.6% of NAV, made four such one-share trades.
                    # Raise min_trade_value or the band to suppress those.
                    if gap_after >= gap_now:
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
                # Without a policy lifetime the order is bounded by its target,
                # which is withdrawn at this age at the latest.
                fresh.now + (self.policy.lifetime or self.policy.max_target_age),
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
            except OrderRefused as error:
                # The platform declined the order outright, so nothing is pending.
                # Recorded as rejected: this target does not ask for it again.
                entry.status = "rejected"
                self._record(
                    "ORDER_REFUSED",
                    target_id=target.metadata.view_id,
                    instrument=instrument.value,
                    intent_id=identity,
                    reason=str(error),
                )
                continue
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
