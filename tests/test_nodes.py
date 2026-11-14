"""Built-in nodes through the runner and the memory adapter."""

from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryAdapter, Sessions
from sugarloaf.core.data import Bar
from sugarloaf.core.errors import ContractError
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import PositionView, ReturnView, ScoreView
from sugarloaf.execution.contracts import ExecutionPolicy
from sugarloaf.nodes.positions import PortfolioConstraintNode, PositionNetter
from sugarloaf.nodes.simple import MeanViewAggregator, PositiveSignalPositions
from sugarloaf.nodes.technical import EMASpread, RSIReversion, SMATrend
from sugarloaf.portfolio.constraints import PortfolioConstraints
from sugarloaf.runtime import GraphRunner

SPY, QQQ = InstrumentId("SPY"), InstrumentId("QQQ")
DAY = timedelta(days=1)
T0 = datetime(2025, 1, 6, 21, tzinfo=UTC)


def sessions(days=40):
    return Sessions(
        regular=tuple(
            (T0 + k * DAY - timedelta(hours=6, minutes=30), T0 + k * DAY)
            for k in range(-days, days)
        )
    )


def bar(instrument, end, close):
    return Bar(instrument, end - DAY, end, close, close * 1.01, close * 0.99, close, 1000.0)


def run_scores(node, closes, instrument=SPY):
    """Feed daily closes through a score node alone and return its archived views."""
    graph = Graph().add_node("signal", node).set_output("signal", "views").compile()
    adapter = MemoryAdapter(T0 - len(closes) * DAY, calendars={instrument: sessions()})
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    for k, close in enumerate(closes):
        end = T0 - (len(closes) - 1 - k) * DAY
        adapter.deliver(end, [bar(instrument, end, close)])
    return runner, adapter


def test_ema_spread_scores_after_both_averages_are_ready():
    node = EMASpread(SPY, fast=2, slow=3, horizon=1)
    runner, adapter = run_scores(node, [10, 11, 12, 13])
    views = list(runner.archive)
    # Ready after three bars (the slow period); the fourth bar restates a new view.
    assert [v.received_at for v in views] == [T0 - DAY, T0]
    last = views[-1].view
    assert last.statement.value > 0
    # A one-session horizon ends at the next regular close on the calendar.
    assert last.target.time.end == T0 + DAY
    assert runner.diagnostics[0].code == "DATA_NOT_READY"


def test_rsi_reversion_is_positive_after_falls():
    node = RSIReversion(SPY, period=3, threshold=50, horizon=DAY)
    runner, _ = run_scores(node, [10, 9, 8, 7, 6])
    assert list(runner.archive)[-1].view.statement.value > 0


def test_sma_trend_compares_close_with_average():
    node = SMATrend(SPY, period=2, horizon=DAY)
    runner, _ = run_scores(node, [10, 10, 20])
    assert list(runner.archive)[-1].view.statement.value == pytest.approx(20 / 15 - 1)


def test_technical_nodes_reject_invalid_settings():
    with pytest.raises(ContractError):
        EMASpread(SPY, fast=5, slow=5)
    with pytest.raises(ContractError):
        RSIReversion(SPY, threshold=101)
    with pytest.raises(ContractError):
        SMATrend(SPY, period=0)


def test_full_trading_graph_trades_when_the_spread_is_positive():
    config = dict(fast=2, slow=3, horizon=1)
    graph = Graph().add_node("signal", EMASpread(SPY, **config))
    graph.add_node("positions", PositiveSignalPositions((SPY,), budget=0.5))
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    graph.connect("signal", "views", "positions", "views")
    graph.connect("positions", "positions", "constraints", "positions")
    compiled = graph.set_output("constraints", "positions").compile()
    adapter = MemoryAdapter(T0 - 5 * DAY, calendars={SPY: sessions()})
    runner = GraphRunner(compiled, adapter, policy=ExecutionPolicy(rebalance_band=0))
    adapter.attach(runner)
    closes = [10, 11, 12, 13, 5]
    for k, close in enumerate(closes):
        end = T0 - (len(closes) - 1 - k) * DAY
        adapter.deliver(end, [bar(SPY, end, close)])
    # Bought half the account at 12, then exited at 5 when the spread turned negative.
    orders = [(o.quantity, o.status) for o in runner.orders]
    assert orders[0] == (pytest.approx(100000 * 0.5 / 12, abs=1), "filled")
    assert orders[-1][0] < 0 and adapter.holdings == {}
    target = runner.last_target
    assert target.positions[SPY] == 0 and target.attribution == {}


def test_mean_aggregator_averages_compatible_claims_and_netter_sums_branches():
    class Pair:
        ports = {}
        outputs = {"views": None}

    now = T0
    interval = Interval(now, now + DAY)

    def view(node, value, error=0.1):
        return ReturnView.create(
            SPY, interval, value, error, Metadata(f"{node}:1", node, now, now + DAY)
        )

    class Fixed:
        ports = {}
        inputs = ()
        outputs = {"views": OutputSpec((ReturnView,))}

        def __init__(self, value):
            self.value = value

        def on_scheduled(self, ctx, name, views):
            return {"views": (view(ctx.node_id, self.value),)}

    from sugarloaf.core.inputs import Schedule

    a, b = Fixed(0.02), Fixed(0.04)
    a.schedules = (Schedule("a", at=__import__("datetime").time(10, 0)),)
    b.schedules = (Schedule("b", at=__import__("datetime").time(10, 1)),)
    graph = Graph().add_node("a", a).add_node("b", b).add_node("mean", MeanViewAggregator())
    graph.connect("a", "views", "mean", "views").connect("b", "views", "mean", "views")
    compiled = graph.set_output("mean", "views").compile()
    adapter = MemoryAdapter(now)
    runner = GraphRunner(compiled, adapter)
    adapter.attach(runner)
    adapter.fire("a")
    adapter.fire("b")
    (averaged,) = runner.outputs("mean", "views")
    assert averaged.statement.value == pytest.approx(0.03)
    assert set(averaged.metadata.parents) == {"a:1", "b:1"}


def test_position_netter_and_constraint_node_over_the_account():
    class Branch:
        ports = {}
        inputs = ()

        def __init__(self, scope, positions, units="weight"):
            self.scope, self.positions, self.units = scope, positions, units
            self.outputs = {"positions": OutputSpec((PositionView,))}
            from sugarloaf.core.inputs import Schedule

            self.schedules = (Schedule(scope, at=__import__("datetime").time(10, 0)),)

        def on_scheduled(self, ctx, name, views):
            return {
                "positions": (
                    PositionView(
                        self.positions, self.units, self.scope,
                        Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time,
                                 ctx.time + DAY),
                    ),
                )
            }

    graph = Graph().add_node("model", Branch("model", {SPY: 0.2, QQQ: 0.1}))
    graph.add_node("direct", Branch("direct", {SPY: 100.0}, "quantity"))
    graph.add_node("net", PositionNetter({"model": 0.65, "direct": 0.1}))
    graph.add_node("check", PortfolioConstraintNode(PortfolioConstraints(gross_limit=0.9)))
    graph.connect("model", "positions", "net", "model")
    graph.connect("direct", "positions", "net", "direct")
    graph.connect("net", "positions", "check", "positions")
    compiled = graph.set_output("check", "positions").compile()
    adapter = MemoryAdapter(T0)
    adapter.set_price(SPY, 50.0)
    adapter.set_price(QQQ, 10.0)
    runner = GraphRunner(compiled, adapter, policy=ExecutionPolicy(rebalance_band=0))
    adapter.attach(runner)
    adapter.fire("model")
    assert runner.last_target is None  # the direct branch is still missing
    adapter.fire("direct")
    target = runner.last_target
    # 100 shares at 50 on a 100,000 account is 5%; with the model's 20% that is 25% SPY.
    assert target.positions[SPY] == pytest.approx(0.25)
    assert target.attribution["direct"][SPY] == pytest.approx(0.05)
    assert adapter.holdings[SPY] == pytest.approx(500)
    assert adapter.holdings[QQQ] == pytest.approx(1000)


def test_positive_signal_positions_needs_every_instrument():
    from sugarloaf.core.node import NodeInputs
    from sugarloaf.runtime.runner import NodeContext

    class Stub:
        steps = 1

    ctx = NodeContext(Stub(), "p", T0)
    node = PositiveSignalPositions((SPY, QQQ), budget=1.0)
    view = ScoreView.__mro__  # noqa: F841  (ScoreView stays importable for the node)
    from sugarloaf.core.views import Score, return_target

    only_spy = ScoreView(
        return_target(SPY, Interval(T0, T0 + DAY)), Score(1.0, "c"),
        Metadata("s:1", "s", T0, T0 + DAY),
    )
    from sugarloaf.core.errors import NotReady

    with pytest.raises(NotReady):
        node.update(ctx, None, NodeInputs({"views": (only_spy,)}))
    with pytest.raises(ContractError):
        PositiveSignalPositions((SPY, SPY))
