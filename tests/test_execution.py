from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sugarloaf.adapters.memory.gateway import MemoryGateway
from sugarloaf.adapters.memory.services import MemoryServices
from sugarloaf.core.errors import ContractError, NotReady, SnapshotError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.services import AccountSnapshot, OpenOrder, PortfolioConstraints
from sugarloaf.core.types import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.execution import ExecutionEngine, ExecutionPolicy, OrderIntent, OrderUpdate, RiskGate
from sugarloaf.execution.engine import ExecutionState, IntentState
from sugarloaf.market.contracts import Capability, Instrument

NOW = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)
A, B = InstrumentId("A"), InstrumentId("B")


def setup(holdings=None, cash=10000, nav=10000):
    account = AccountSnapshot(holdings or {}, nav, {"USD": cash}, {A: 100, B: 100})
    calendar = {
        i: {"regular": ((NOW - timedelta(hours=1), NOW + timedelta(hours=5)),)} for i in (A, B)
    }
    services = MemoryServices((Instrument(A, "A"), Instrument(B, "B")), account, calendars=calendar)
    services.apply(EventBatch(1, NOW))
    gateway = MemoryGateway(services)
    engine = ExecutionEngine(gateway)
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
    services, gateway, engine = setup()
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
    services.constraints = PortfolioConstraints(
        allow_short=True, min_weight=-1, budget=2, gross_limit=2, net_min=-1, net_max=0.5
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


def test_positive_minimum_net_does_not_deadlock_entry_from_cash():
    services, gateway, engine = setup()
    services.constraints = PortfolioConstraints(net_min=0.2)
    engine.accept(target(1, {A: 10, B: 10}))
    assert len(gateway.orders) == 2
    assert all(order.quantity == 10 for order in gateway.orders.values())


@pytest.mark.parametrize("reason", ["expiry", "supersession", "withdrawal"])
def test_failed_cancellation_retries_without_releasing_pending_exposure(reason):
    services, gateway, engine = setup()
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
    assert len(snapshots) == (1 if route == "no_target" else 2)
    for ctx in snapshots:
        with pytest.raises(SnapshotError, match="released"):
            ctx.check()
