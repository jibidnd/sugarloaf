from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sugarloaf.adapters.memory.gateway import MemoryGateway
from sugarloaf.adapters.memory.services import MemoryServices
from sugarloaf.core.errors import ContractError, NotReady, SnapshotError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.services import AccountSnapshot, OpenOrder
from sugarloaf.core.views import PositionView
from sugarloaf.execution import ExecutionEngine, ExecutionPolicy, OrderIntent, OrderUpdate, RiskGate
from sugarloaf.execution.engine import ExecutionState, IntentState
from sugarloaf.market.contracts import Capability, Instrument
from sugarloaf.portfolio.constraints import PortfolioConstraints

NOW = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)
A, B = InstrumentId("A"), InstrumentId("B")


# Tests of order expiry opt in to a cancel timer; the policy has none by default.
FIVE_MINUTES = ExecutionPolicy(lifetime=timedelta(minutes=5))


def setup(holdings=None, cash=10000, nav=10000, policy=None):
    account = AccountSnapshot(holdings or {}, nav, {"USD": cash}, {A: 100, B: 100})
    calendar = {
        i: {"regular": ((NOW - timedelta(hours=1), NOW + timedelta(hours=5)),)} for i in (A, B)
    }
    services = MemoryServices((Instrument(A, "A"), Instrument(B, "B")), account, calendars=calendar)
    services.apply(EventBatch(1, NOW))
    gateway = MemoryGateway(services)
    engine = ExecutionEngine(gateway, policy)
    gateway.on_update = engine.on_order_update
    return services, gateway, engine


def target(number, positions):
    return PositionView(
        positions,
        "quantity",
        "account",
        Metadata(f"t{number}", "terminal", NOW, NOW + timedelta(hours=1)),
    )


def test_partial_fill_replay_and_supersession_wait_for_cancel():
    services, gateway, engine = setup()
    first = target(1, {A: 50})
    engine.accept(first)
    gateway.fill("1", 20)
    engine.accept(first)
    assert len(gateway.orders) == 1
    engine.accept(target(2, {A: 10}))
    assert gateway.cancel_requests == {"1"}
    assert len(gateway.orders) == 1
    gateway.fill("1", 5)  # Late fill during pending cancellation.
    gateway.confirm_cancel("1")
    engine.reconcile()
    assert gateway.orders["2"].quantity == -15
    gateway.fill("2", -15)
    engine.reconcile()
    assert services.account.holdings[A] == 10
    assert len(gateway.orders) == 2


def test_invalid_target_cancels_orders_but_preserves_filled_holdings():
    services, gateway, engine = setup()
    engine.accept(target(1, {A: 50}))
    gateway.fill("1", 10)
    engine.invalidate()
    gateway.confirm_cancel("1")
    engine.reconcile()
    assert services.account.holdings[A] == 10
    assert len(gateway.orders) == 1


def test_pending_sell_cannot_fund_buy_until_fill():
    services, gateway, engine = setup({A: 100}, cash=0)
    engine.accept(target(1, {A: 0, B: 100}))
    assert len(gateway.orders) == 1
    assert gateway.orders["1"].instrument == A
    gateway.fill("1", -100)
    engine.reconcile()
    assert gateway.orders["2"].instrument == B


def test_unknown_reserved_order_counts_as_possible_exposure():
    services, _, engine = setup()
    first = OrderIntent("reserved", "t1", A, 100, NOW, NOW + timedelta(minutes=5))
    engine.state.intents[first.intent_id] = IntentState(first, status="unknown")
    second = replace(first, intent_id="second", instrument=B)
    with pytest.raises(NotReady, match="exposure"):
        RiskGate().check(
            second, target(1, {A: 100, B: 100}), services.context(), ExecutionPolicy(), engine.state
        )


def test_expired_order_waits_for_cancel_and_expired_view_holds():
    services, gateway, engine = setup(policy=FIVE_MINUTES)
    engine.accept(target(1, {A: 30}))
    services.apply(EventBatch(2, NOW + timedelta(minutes=6)))
    engine.reconcile()
    assert gateway.cancel_requests == {"1"}
    assert len(gateway.orders) == 1
    gateway.fill("1", 10)
    gateway.confirm_cancel("1")
    services.apply(EventBatch(3, NOW + timedelta(hours=2)))
    engine.reconcile()
    assert engine.state.target is None
    assert services.account.holdings[A] == 10


def test_risk_permits_reduction_from_existing_exposure_breach():
    services, _, _ = setup({A: 110}, cash=-1000)
    intent = OrderIntent("reduce", "t1", A, -10, NOW, NOW + timedelta(minutes=5))
    RiskGate().check(
        intent, target(1, {A: 100}), services.context(), ExecutionPolicy(), ExecutionState()
    )


def test_pending_external_orders_are_in_risk_envelope():
    services, _, engine = setup()
    services.set_account(replace(services.account, open_orders=(OpenOrder("external", A, 80),)))
    intent = OrderIntent("new", "t1", B, 30, NOW, NOW + timedelta(minutes=5))
    with pytest.raises(NotReady):
        RiskGate().check(
            intent, target(1, {B: 30}), services.context(), ExecutionPolicy(), engine.state
        )


def test_cover_needs_no_new_borrow_but_cannot_worsen_pending_net_exposure():
    services, _, engine = setup({A: -50, B: 50})
    # A negative min_weight is what permits the short; no separate switch exists.
    services.constraints = PortfolioConstraints(
        min_weight=-1, gross_limit=2, net_min=-1, net_max=0.5
    )
    intent = OrderIntent("cover", "t1", A, 50, NOW, NOW + timedelta(minutes=5))
    RiskGate().check(intent, target(1, {A: 0}), services.context(), ExecutionPolicy(), engine.state)
    services.set_account(replace(services.account, open_orders=(OpenOrder("external", B, 50),)))
    with pytest.raises(NotReady, match="EXPOSURE"):
        RiskGate().check(
            intent, target(1, {A: 0}), services.context(), ExecutionPolicy(), engine.state
        )


def test_turnover_is_enforced_on_direct_execution_route():
    services, _, engine = setup()
    services.constraints = PortfolioConstraints(turnover_limit=0)
    intent = OrderIntent("new", "t1", A, 10, NOW, NOW + timedelta(minutes=5))
    with pytest.raises(NotReady, match="TURNOVER"):
        RiskGate().check(
            intent, target(1, {A: 10}), services.context(), ExecutionPolicy(), engine.state
        )


def test_weight_translation_rechecks_prices_before_submission():
    services, gateway, engine = setup()
    original_context = gateway.context
    reads = 0

    def changing_context():
        nonlocal reads
        reads += 1
        if reads == 2:
            services.set_account(replace(services.account, prices={A: 200, B: 100}))
        return original_context()

    gateway.context = changing_context
    view = replace(target(1, {A: 0.5}), units="weight")
    engine.accept(view)
    assert gateway.orders["1"].quantity == 25


def test_explicit_minimum_rebalance_value_preserves_zero_exit():
    services, gateway, _ = setup({A: 10}, cash=9000)
    engine = ExecutionEngine(gateway, ExecutionPolicy(min_trade_value=200))
    gateway.on_update = engine.on_order_update
    engine.accept(target(1, {A: 11}))
    assert not gateway.orders
    services.set_account(replace(services.account, holdings={A: 1}, cash={"USD": 9900}))
    engine.accept(target(2, {A: 0}))
    assert gateway.orders["1"].quantity == -1


def test_rebalance_band_ignores_weight_drift_but_not_entries_exits_or_quantities():
    # Holding 20 of A at 100 is 20% of a 10,000 account; the band is 0.25% = USD25.
    services, gateway, engine = setup({A: 20}, cash=8000)
    weight = replace(target(1, {A: 0.2}), units="weight")
    engine.accept(weight)
    assert not gateway.orders  # Already on target.
    # The price drifts up 0.5%: holding 20 is now a 20.08% weight and exactly
    # 19.92 shares are wanted. Whole lots would say "sell one share" (1% of NAV),
    # but the account is really only 0.08 shares (USD8) off, inside the band.
    services.set_account(replace(services.account, nav=10010, prices={A: 100.5, B: 100}))
    engine.reconcile()
    assert not gateway.orders
    # A real gap of 1.08 shares (about 1.1% of NAV) is outside the band and trades...
    services.set_account(replace(services.account, holdings={A: 21}))
    engine.reconcile()
    assert gateway.orders["1"].quantity == -2
    gateway.fill("1", -2)
    # ...while a gap smaller than the band is left alone for a weight target.
    wide = ExecutionEngine(gateway, ExecutionPolicy(rebalance_band=0.05))
    gateway.on_update = wide.on_order_update
    services.set_account(replace(services.account, holdings={A: 21}))
    wide.accept(replace(target(2, {A: 0.2}), units="weight"))
    assert len(gateway.orders) == 1
    # A quantity target is an exact instruction: the same small gap is traded.
    wide.accept(target(3, {A: 19}))
    assert gateway.orders["2"].quantity == -2
    gateway.fill("2", -2)
    # An exit to zero weight is never suppressed, however small the position.
    services.set_account(replace(services.account, holdings={A: 1}))
    wide.accept(replace(target(4, {A: 0}), units="weight"))
    assert gateway.orders["3"].quantity == -1


def test_weight_target_order_never_overshoots_when_one_lot_exceeds_the_band():
    # 50 shares at 100 in a 10,000 account. One share is 1% of NAV, four times the
    # default 0.25% band, so the band alone cannot stop a one-share correction.
    services, gateway, engine = setup({A: 50}, cash=5000)
    weight = replace(target(1, {A: 0.5}), units="weight")
    # Price rises 2%: NAV 10,100 and exactly 49.51 shares are wanted. The account
    # is 0.49 shares over target, about 0.5% of NAV and so outside the band. But
    # selling one would leave it 0.51 under, further away than it is now, so no
    # order is sent.
    services.set_account(replace(services.account, nav=10100, prices={A: 102, B: 100}))
    engine.accept(weight)
    assert not gateway.orders
    # Price rises to 104: exactly 49.04 are wanted, 0.96 shares over target.
    # Selling one now leaves the account only 0.04 off, so the order is sent.
    services.set_account(replace(services.account, nav=10200, prices={A: 104, B: 100}))
    engine.reconcile()
    assert gateway.orders["1"].quantity == -1


def test_missing_price_skips_only_that_instrument():
    # B has no mark, as a newly added universe member might not. Before this was
    # handled, the missing price raised out of reconciliation and stopped the run.
    services, gateway, engine = setup()
    services.set_account(replace(services.account, prices={A: 100}))
    engine.accept(target(1, {A: 10, B: 10}))
    assert [order.instrument for order in gateway.orders.values()] == [A]
    skipped = [d for d in engine.diagnostics if d["code"] == "VALUATION_UNAVAILABLE"]
    assert skipped and skipped[0]["instrument"] == "B"


def test_finished_intents_of_superseded_targets_leave_the_ledger():
    _, gateway, engine = setup()
    for number in range(1, 4):
        engine.accept(target(number, {A: number}))
        gateway.fill(str(number), 1)
    # Only the current target's intent remains, however many targets came before.
    assert [entry.intent.target_id for entry in engine.state.intents.values()] == ["t3"]


def test_positive_minimum_net_does_not_deadlock_entry_from_cash():
    services, gateway, engine = setup()
    services.constraints = PortfolioConstraints(net_min=0.2)
    engine.accept(target(1, {A: 10, B: 10}))
    assert len(gateway.orders) == 2
    assert all(order.quantity == 10 for order in gateway.orders.values())


@pytest.mark.parametrize("reason", ["expiry", "supersession", "withdrawal"])
def test_failed_cancellation_retries_without_releasing_pending_exposure(reason):
    services, gateway, engine = setup(policy=FIVE_MINUTES)
    engine.accept(target(1, {A: 30}))
    original_cancel = gateway.cancel
    attempts = []

    def transient_failure(order_id):
        attempts.append(order_id)
        if len(attempts) == 1:
            raise RuntimeError("Temporary cancellation failure")
        original_cancel(order_id)

    gateway.cancel = transient_failure
    with pytest.raises(RuntimeError, match="Temporary cancellation failure"):
        if reason == "expiry":
            services.apply(EventBatch(2, NOW + timedelta(minutes=6)))
            engine.reconcile()
        elif reason == "supersession":
            engine.accept(target(2, {A: 10}))
        else:
            engine.invalidate()
    entry = next(iter(engine.state.intents.values()))
    assert not entry.cancel_requested
    assert entry.status == "open"
    assert engine.diagnostics[-1]["code"] == "CANCEL_FAILED"
    engine.reconcile()
    assert attempts == ["1", "1"]
    assert entry.cancel_requested
    assert len(gateway.orders) == 1
    # Even a retried accepted request reserves the original outstanding order.
    gateway.fill("1", 5)
    gateway.confirm_cancel("1")
    engine.reconcile()
    if reason == "withdrawal":
        assert len(gateway.orders) == 1
        assert services.account.holdings[A] == 5
    else:
        assert gateway.orders["2"].quantity == (25 if reason == "expiry" else 5)


def test_asynchronous_cancel_rejection_retries_and_still_waits_for_confirmation():
    _, gateway, engine = setup()
    engine.accept(target(1, {A: 30}))
    cancel_calls = []
    request_outcome = None

    def cancel(order_id):
        nonlocal request_outcome
        cancel_calls.append(order_id)
        request_outcome = None  # The replacement request has not been processed.

    gateway.cancel = cancel
    gateway.cancel_status = lambda order_id: request_outcome
    engine.accept(target(2, {A: 10}))
    engine.reconcile()  # Request still unprocessed.
    assert cancel_calls == ["1"]
    request_outcome = Capability(True)
    engine.reconcile()  # Accepted is still not canceled.
    assert cancel_calls == ["1"]
    request_outcome = Capability(False, "Brokerage rejected cancellation")
    engine.reconcile()
    assert cancel_calls == ["1", "1"]
    assert len(gateway.orders) == 1
    assert any(d["code"] == "CANCEL_REJECTED" for d in engine.diagnostics)
    engine.reconcile()
    assert cancel_calls == ["1", "1"]  # No duplicate while the retry is unprocessed.
    request_outcome = Capability(True)
    gateway.confirm_cancel("1")
    engine.reconcile()
    assert gateway.orders["2"].quantity == 10


def test_unavailable_cancel_response_does_not_authorize_duplicate_requests():
    _, gateway, engine = setup()
    engine.accept(target(1, {A: 30}))
    engine.accept(target(2, {A: 10}))

    def unavailable_status(order_id):
        raise RuntimeError("Cancellation response unavailable")

    gateway.cancel_status = unavailable_status
    with pytest.raises(RuntimeError, match="Cancellation response unavailable"):
        engine.reconcile()
    entry = next(iter(engine.state.intents.values()))
    assert entry.cancel_requested and entry.status == "open"
    assert len(gateway.orders) == 1

    gateway.cancel_status = lambda order_id: Capability(True)
    gateway.confirm_cancel("1")
    engine.reconcile()
    assert gateway.orders["2"].quantity == 10


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"filled": float("nan")}, "finite"),
        ({"filled": float("inf")}, "finite"),
        ({"status": "brokerage_typo"}, "Unknown order feedback status"),
        ({"time": NOW.replace(tzinfo=None)}, "timezone aware"),
        ({"order_id": ""}, "platform order identity"),
        ({"status": "filled", "filled": 0}, "nonzero cumulative"),
        ({"status": "partial", "filled": 0}, "nonzero cumulative"),
    ],
)
def test_order_feedback_rejects_malformed_boundary_values(changes, message):
    values = dict(order_id="1", intent_id="t1:intent:1", status="open", filled=0, time=NOW)
    values.update(changes)
    with pytest.raises(ContractError, match=message):
        OrderUpdate(**values)


@pytest.mark.parametrize("quantity", [30, -30])
def test_order_feedback_checks_sign_limits_completion_and_delayed_status(quantity):
    _, _, engine = setup()
    identity = "local"
    intent = OrderIntent(identity, "t1", A, quantity, NOW, NOW + timedelta(minutes=5))
    entry = IntentState(intent, order_id="1", status="open")
    engine.state.intents[identity] = entry
    # The intent belongs to the current target, so it stays in the ledger after
    # it finishes and its late feedback can still be checked below.
    engine.state.target = target(1, {A: quantity})

    for status, filled, message in (
        ("partial", -quantity / 2, "wrong sign"),
        ("filled", quantity / 2, "complete submitted quantity"),
        ("partial", quantity * 2, "exceed submitted quantity"),
    ):
        with pytest.raises(ContractError, match=message):
            engine.on_order_update(OrderUpdate("1", identity, status, filled, NOW))
        assert entry.filled == 0 and entry.status == "open"

    engine.on_order_update(OrderUpdate("1", identity, "partial", quantity / 2, NOW))
    engine.on_order_update(OrderUpdate("1", identity, "open", 0, NOW))
    assert entry.filled == quantity / 2  # Replayed older feedback cannot rewind fills.
    engine.on_order_update(OrderUpdate("1", identity, "canceled", quantity / 2, NOW))
    engine.on_order_update(OrderUpdate("1", identity, "partial", quantity * 0.75, NOW))
    assert entry.status == "canceled" and entry.filled == quantity * 0.75
    # QC can queue an older event while its ticket already contains complete fills.
    engine.on_order_update(OrderUpdate("1", identity, "partial", quantity, NOW))
    engine.on_order_update(OrderUpdate("1", identity, "canceled", quantity, NOW))
    assert entry.status == "filled" and entry.filled == quantity


def test_small_quantity_partial_feedback_is_not_a_complete_fill():
    _, _, engine = setup()
    intent = OrderIntent("local", "t1", A, 1e-9, NOW, NOW + timedelta(minutes=5))
    entry = IntentState(intent, order_id="1", status="open")
    engine.state.intents["local"] = entry
    engine.on_order_update(OrderUpdate("1", "local", "partial", 1e-10, NOW))
    assert entry.status == "partial"
    assert entry.filled == 1e-10


@pytest.mark.parametrize("route", ["submit", "no_target", "submission_failure"])
def test_execution_releases_gateway_snapshots_after_every_reconciliation_route(route):
    _, gateway, engine = setup()
    original_context = gateway.context
    snapshots = []

    def tracking_context():
        ctx = original_context()
        snapshots.append(ctx)
        return ctx

    gateway.context = tracking_context
    if route == "submission_failure":
        def fail_submit(intent):
            raise RuntimeError("Submission response lost")

        gateway.submit = fail_submit
        with pytest.raises(RuntimeError, match="Submission response lost"):
            engine.accept(target(1, {A: 30}))
        assert next(iter(engine.state.intents.values())).status == "unknown"
    elif route == "submit":
        engine.accept(target(1, {A: 30}))
        assert gateway.orders["1"].quantity == 30
    else:
        engine.reconcile()
    # With no target there is nothing to size, so no broker snapshot is taken.
    assert len(snapshots) == (0 if route == "no_target" else 2)
    for ctx in snapshots:
        with pytest.raises(SnapshotError, match="released"):
            ctx.check()


def test_policy_names_one_of_three_order_types_and_has_no_cancel_timer_by_default():
    for order_type in ("market", "market_on_open", "market_on_close"):
        assert ExecutionPolicy(order_type=order_type).lifetime is None
    explicit = ExecutionPolicy(order_type="market_on_close", lifetime=timedelta(hours=7))
    assert explicit.lifetime == timedelta(hours=7)
    with pytest.raises(ContractError, match="Unknown order type"):
        ExecutionPolicy(order_type="limit")
    assert ExecutionPolicy().time_in_force == "gtc"
    assert ExecutionPolicy(time_in_force="day").time_in_force == "day"
    with pytest.raises(ContractError, match="Unknown time in force"):
        ExecutionPolicy(time_in_force="fok")
    with pytest.raises(ContractError, match="positive"):
        ExecutionPolicy(lifetime=timedelta(0))


def test_an_order_the_platform_holds_overnight_is_not_cancelled_by_a_later_reconciliation():
    # Seen natively: daily-bar orders sent at the close wait for the next open, and
    # a corporate-action batch at midnight reconciles in between. A five-minute
    # default lifetime cancelled every one of them.
    services, gateway, engine = setup()
    held = PositionView(
        {A: 5}, "quantity", "account", Metadata("t", "terminal", NOW, NOW + timedelta(days=2))
    )
    engine.accept(held)
    services.apply(EventBatch(2, NOW + timedelta(hours=8)))
    engine.accept(held)
    assert gateway.cancel_requests == set()
    assert len(gateway.orders) == 1


def test_intent_carries_the_policy_order_type_and_lifetime_to_the_gateway():
    services, gateway, _ = setup()
    engine = ExecutionEngine(gateway, ExecutionPolicy(order_type="market_on_close"))
    gateway.on_update = engine.on_order_update
    engine.accept(target(1, {A: 5}))
    (intent,) = gateway.intents.values()
    assert intent.order_type == "market_on_close"
    # No policy lifetime: bounded by the oldest a target may get.
    assert intent.expires_at == NOW + engine.policy.max_target_age


def test_an_order_decided_outside_the_session_is_sent_and_left_to_the_platform():
    # A decision on the daily bar arrives at the close. Whether the order then
    # waits for the next open, fills at the close or is refused is the platform's
    # rule, so the engine sends it instead of dropping it.
    services, gateway, engine = setup()
    services.apply(EventBatch(2, NOW + timedelta(hours=6)))
    assert not services.context().instruments.is_open(A, NOW + timedelta(hours=6), "regular")
    late = PositionView(
        {A: 5},
        "quantity",
        "account",
        Metadata("late", "terminal", NOW + timedelta(hours=6), NOW + timedelta(days=1)),
    )
    engine.accept(late)
    assert [order.quantity for order in gateway.orders.values()] == [5]
    assert not [d for d in engine.diagnostics if d["code"] == "RISK_REJECTED"]


def test_a_platform_refusal_is_a_rejected_intent_that_reserves_nothing_and_is_not_retried():
    from sugarloaf.core.errors import OrderRefused

    services, gateway, engine = setup()
    attempts = []

    def refuse(intent):
        attempts.append(intent.instrument)
        raise OrderRefused("too late for the closing auction")

    gateway.submit = refuse
    first = target(1, {A: 5, B: 5})
    engine.accept(first)  # Does not raise: the outcome is known.
    # One refusal does not stop the other instruments of the target.
    assert attempts == [A, B]
    assert {entry.status for entry in engine.state.intents.values()} == {"rejected"}
    refusals = [d for d in engine.diagnostics if d["code"] == "ORDER_REFUSED"]
    assert [d["instrument"] for d in refusals] == ["A", "B"]
    assert "too late" in refusals[0]["reason"]
    engine.accept(first)
    assert attempts == [A, B]  # The same target does not ask again.
    engine.accept(target(2, {A: 5}))
    assert attempts == [A, B, A]  # A new target does.


def test_an_older_order_filled_while_another_is_being_sent_is_not_counted_twice():
    # Seen natively on daily bars: sending one order made the platform fill the
    # orders already waiting. Their fills were in holdings at the next broker read
    # while their feedback was still queued, so each was also counted as pending
    # and the engine sent an order to reverse it.
    services, gateway, engine = setup()
    services.set_account(replace(services.account, prices={B: 100}))
    held = target(1, {A: 5, B: 10})
    engine.accept(held)  # A has no mark yet, so only B's order goes out and waits.
    assert [(o.instrument, o.quantity) for o in gateway.orders.values()] == [(B, 10)]
    send = gateway.submit

    def send_then_fill_the_waiting_order(intent):
        order_id = send(intent)
        gateway.fill("1", 10)  # The platform fills B before the call for A returns.
        return order_id

    gateway.submit = send_then_fill_the_waiting_order
    services.set_account(replace(services.account, prices={A: 100, B: 100}))
    engine.reconcile()  # A is sent first (smaller gap); B is looked at after its fill.
    assert [(o.instrument, o.quantity) for o in gateway.orders.values()] == [(B, 10), (A, 5)]
    assert services.account.holdings[B] == 10
    assert engine.state.intents["t1:intent:1"].status == "filled"


def test_a_split_keeps_a_weight_target_and_sizes_only_that_instruments_waiting_order_again():
    from sugarloaf.core.events import Event, LifecycleEvent
    from sugarloaf.core.graph import Graph
    from sugarloaf.core.node import NodeOutputs, OutputSpec
    from sugarloaf.execution.session import TradingSession
    from sugarloaf.market.contracts import Trigger

    class Weights:
        inputs = {}
        outputs = {"positions": OutputSpec((PositionView,))}
        requirements = ()
        triggers = (Trigger("data"),)

        def evaluate(self, inputs, ctx):
            end = ctx.now + timedelta(days=2)
            meta = Metadata(f"w:{ctx.snapshot_id}", ctx.node_id, ctx.now, end)
            weights = PositionView({A: 0.1, B: 0.1}, "weight", "account", meta)
            return NodeOutputs({"positions": (weights,)})

    services, gateway, engine = setup()
    graph = Graph().add_node("w", Weights()).set_output("w", "positions").compile()
    session = TradingSession(graph, services, engine)
    session.process(EventBatch(2, NOW, (Event("data"),)))
    assert [(o.instrument, o.quantity) for o in gateway.orders.values()] == [(A, 10), (B, 10)]
    target_id = engine.state.target.metadata.view_id
    # A splits while both orders wait. "10% of NAV" still stands, so the target is
    # kept; the order for A was counted in old shares and is cancelled. B's is not.
    at = NOW + timedelta(seconds=1)
    split = Event("lifecycle", A, payload=LifecycleEvent("split", A, at, factor=0.5))
    session.process(EventBatch(3, at, (split,)))
    assert engine.state.target.metadata.view_id == target_id
    assert gateway.cancel_requests == {"1"}
    assert len(gateway.orders) == 2  # No replacement until the cancellation is confirmed.
    gateway.confirm_cancel("1")
    engine.reconcile()
    assert [(o.instrument, o.quantity, o.status) for o in gateway.orders.values()] == [
        (A, 10, "canceled"), (B, 10, "open"), (A, 10, "open"),
    ]
