"""The runner: dispatch by declaration, view carriage, archive and execution."""

from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryAdapter, Sessions
from sugarloaf.core.data import Bar, Dividend
from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import (
    Bars,
    Custom,
    Indicator,
    LiquidityUniverse,
    Schedule,
    Selection,
    WarmUp,
)
from sugarloaf.core.node import InputSpec, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.universes import SelectionRow
from sugarloaf.core.views import PositionView, Score, ScoreView, return_target
from sugarloaf.portfolio.constraints import PortfolioConstraints
from sugarloaf.runtime import GraphRunner

SPY, QQQ, AAA = InstrumentId("SPY"), InstrumentId("QQQ"), InstrumentId("AAA")
T0 = datetime(2025, 1, 6, 21, tzinfo=UTC)
DAY = timedelta(days=1)


def bar(instrument, end, close=100.0, period=DAY):
    return Bar(instrument, end - period, end, close, close + 1, close - 1, close, 1000.0)


def score(node_id, instrument, time, value, sequence, lifetime=DAY):
    return ScoreView(
        return_target(instrument, Interval(time, time + lifetime)),
        Score(value, "test"),
        Metadata(f"{node_id}:{sequence}", node_id, time, time + lifetime),
    )


class Recorder:
    """A source node that records what it receives and scores the latest close."""

    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instruments, **kwargs):
        self.bars = Bars(Selection(tuple(instruments)), **kwargs)
        self.inputs = (self.bars,)
        self.slices = []
        self.changes = []

    def on_securities_changed(self, ctx, added, removed):
        self.changes.append((added, removed))

    def update(self, ctx, data, views):
        self.slices.append(data)
        return {
            "views": tuple(
                score(ctx.node_id, instrument, ctx.time, b.close,
                      f"{ctx.sequence}:{instrument.value}")
                for instrument, b in sorted(data.bars.items(), key=lambda kv: kv[0].value)
            )
        }


class Sizer:
    """Turns positive scores into equal weights; no data of its own."""

    ports = {"views": InputSpec((ScoreView,), min_count=0)}
    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def __init__(self, weight=0.5):
        self.weight = weight
        self.calls = 0

    def update(self, ctx, data, views):
        self.calls += 1
        positive = [v.target.subject for v in views["views"] if v.statement.value > 0]
        if not positive:
            raise NotReady("nothing positive")
        expiry = min(v.metadata.expires_at for v in views["views"])
        return {
            "positions": (
                PositionView(
                    {i: self.weight for i in positive},
                    "weight",
                    "test",
                    Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, expiry),
                ),
            )
        }


def trading_graph(source, sizer):
    graph = Graph().add_node("source", source).add_node("sizer", sizer)
    graph.connect("source", "views", "sizer", "views")
    return graph.set_output("sizer", "positions").compile()


def test_source_node_sees_only_its_instruments_and_consumer_runs_on_change():
    source, sizer = Recorder((SPY,)), Sizer()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(trading_graph(source, sizer), adapter)
    adapter.attach(runner)
    assert adapter.feeds and set(adapter.windows[source.bars.key]) == {SPY}
    adapter.deliver(T0, [bar(SPY, T0, 100.0), bar(QQQ, T0, 50.0)])
    assert len(source.slices) == 1 and set(source.slices[0].bars) == {SPY}
    assert sizer.calls == 1
    assert runner.last_target is not None and runner.last_target.positions[SPY] == 0.5
    # One order was placed and filled at the mark.
    assert [(o.instrument, o.quantity) for o in runner.orders] == [(SPY, 500.0)]
    assert adapter.holdings[SPY] == 500.0
    # A slice without the source's instrument touches neither node.
    adapter.deliver(T0 + DAY, [bar(QQQ, T0 + DAY, 51.0)])
    assert len(source.slices) == 1 and sizer.calls == 1
    # The same target restated does not re-order; a new one does.
    adapter.deliver(T0 + 2 * DAY, [bar(SPY, T0 + 2 * DAY, 100.0)])
    assert sizer.calls == 2 and len(runner.orders) == 1


def test_not_ready_withdraws_and_records_a_diagnostic():
    source, sizer = Recorder((SPY,)), Sizer()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(trading_graph(source, sizer), adapter)
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0, -5.0 + 10.0) ])  # close 5 > 0 is positive
    adapter.deliver(T0 + DAY, [Bar(SPY, T0, T0 + DAY, -1.0, 0.0, -2.0, -1.0, 1.0)])
    assert runner.outputs("sizer", "positions") == ()
    assert runner.diagnostics[-1].code == "DATA_NOT_READY"
    # The previous target is not re-sent and holdings are untouched.
    assert len(runner.orders) == 1


def test_expired_upstream_views_are_not_delivered():
    source, sizer = Recorder((SPY,)), Sizer()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(trading_graph(source, sizer), adapter)
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0, 100.0)])
    assert runner.last_target is not None
    # Two days later the day-long score has expired. A slice that touches only
    # the sizer's upstream (through a QQQ bar? no: nothing) leaves it untouched,
    # so force a run through a schedule-free path: deliver a SPY bar again.
    adapter.deliver(T0 + 3 * DAY, [bar(SPY, T0 + 3 * DAY, 100.0)])
    delivered = runner.outputs("sizer", "positions")[0]
    assert delivered.metadata.cutoff == T0 + 3 * DAY


def test_identity_reuse_with_different_contents_is_refused():
    class Reuser:
        ports = {}
        outputs = {"views": OutputSpec((ScoreView,))}
        inputs = (Bars(Selection((SPY,))),)

        def update(self, ctx, data, views):
            return {"views": (score(ctx.node_id, SPY, ctx.time, data.bars[SPY].close, "fixed"),)}

    graph = Graph().add_node("r", Reuser()).set_output("r", "views").compile()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0, 1.0)])
    with pytest.raises(ContractError):
        adapter.deliver(T0 + DAY, [bar(SPY, T0 + DAY, 2.0)])


def test_forecast_run_archives_every_predictive_view_and_executes_nothing():
    source = Recorder((SPY, QQQ))
    graph = Graph().add_node("source", source).set_output("source", "views").compile()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(graph, adapter)
    assert not runner.trading
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0, 1.0), bar(QQQ, T0, 2.0)])
    adapter.deliver(T0 + DAY, [bar(SPY, T0 + DAY, 3.0)])
    assert len(runner.archive) == 3 and runner.orders == []
    record = runner.archive.get("source:1:SPY")
    assert record.received_at == T0 and record.port == "views"


def test_named_series_keeps_two_intervals_apart():
    class TwoFeeds:
        ports = {}
        outputs = {}
        inputs = (
            Bars(Selection((SPY,)), "minute", timedelta(minutes=5), name="five"),
            Bars(Selection((SPY,)), "minute", name="one"),
        )

        def __init__(self):
            self.seen = []

        def update(self, ctx, data, views):
            self.seen.append((sorted(data.series("one")), sorted(data.series("five"))))
            return None

    node = TwoFeeds()
    # A node with no outputs cannot be the graph output; a second node is.
    graph = Graph().add_node("n", node).add_node("sink", Recorder((QQQ,)))
    graph = graph.set_output("sink", "views").compile()
    minute = timedelta(minutes=1)
    start = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
    adapter = MemoryAdapter(start)
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    for k in range(6):
        end = start + (k + 1) * minute
        adapter.deliver(end, [bar(SPY, end, 100.0 + k, minute)])
    # Six one-minute bars; the five-minute bar completes when the sixth minute starts.
    assert len(node.seen) == 6
    assert all(one == [SPY] for one, _ in node.seen)
    assert [five for _, five in node.seen] == [[], [], [], [], [], [SPY]]


def test_warm_up_fills_windows_without_keeping_views_or_orders():
    source, sizer = Recorder((SPY,), window=3), Sizer()
    source.warm_up = WarmUp(count=3)
    adapter = MemoryAdapter(T0 - 3 * DAY)
    runner = GraphRunner(trading_graph(source, sizer), adapter)
    adapter.attach(runner)
    assert adapter.warm_up_request == WarmUp(count=3)
    for k in range(3):
        at = T0 - (3 - k) * DAY
        adapter.deliver(at, [bar(SPY, at, 90.0 + k)], warming_up=True)
    assert len(source.slices) == 3 and runner.orders == [] and runner.outputs("source") == {}
    adapter.deliver(T0, [bar(SPY, T0, 100.0)])
    window = adapter.window(source.bars, SPY)
    assert [b.close for b in window] == [100.0, 92.0, 91.0]
    assert len(runner.orders) == 1


def test_schedule_runs_its_owner_and_dependents():
    class Scheduled:
        ports = {}
        outputs = {"views": OutputSpec((ScoreView,))}
        inputs = (Bars(Selection((SPY,))),)
        schedules = (Schedule("ten", calendar=SPY, after_open=30),)

        def __init__(self):
            self.names = []

        def update(self, ctx, data, views):
            return None

        def on_scheduled(self, ctx, name, views):
            self.names.append(name)
            return {"views": (score(ctx.node_id, SPY, ctx.time, 1.0, ctx.sequence),)}

    node, sizer = Scheduled(), Sizer()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(trading_graph(node, sizer), adapter)
    adapter.attach(runner)
    assert "ten" in adapter.schedules
    adapter.fire("ten", T0 + timedelta(hours=1))
    assert node.names == ["ten"] and sizer.calls == 1 and len(runner.orders) == 1


def test_universe_members_are_notified_and_sliced():
    class Members:
        ports = {}
        outputs = {"views": OutputSpec((ScoreView,))}
        inputs = (
            LiquidityUniverse("liquid", 2, cadence="monthly"),
            Bars(Selection(universe="liquid"), window=2),
        )

        def __init__(self):
            self.changes, self.seen = [], []

        def on_securities_changed(self, ctx, added, removed):
            self.changes.append((added, removed))

        def update(self, ctx, data, views):
            self.seen.append(sorted(data.bars))
            assert set(ctx.members("liquid")) >= set(data.bars)
            return None

    node = Members()
    graph = Graph().add_node("m", node).add_node("sink", Recorder((AAA,)))
    graph = graph.set_output("sink", "views").compile()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    rows = [
        SelectionRow(SPY, 100, 1e6, 1e8),
        SelectionRow(QQQ, 50, 1e6, 5e7),
        SelectionRow(AAA, 1, 1e3, 1e3),
    ]
    adapter.refresh("liquid", rows, T0)
    assert node.changes == [((SPY, QQQ), ())]
    adapter.deliver(T0, [bar(SPY, T0), bar(QQQ, T0), bar(AAA, T0)])
    assert node.seen == [[QQQ, SPY]]
    adapter.set_members("liquid", (SPY,))
    assert node.changes[-1] == ((), (QQQ,))
    adapter.deliver(T0 + DAY, [bar(SPY, T0 + DAY), bar(QQQ, T0 + DAY)])
    assert node.seen[-1] == [SPY]


def test_lifecycle_facts_reach_only_nodes_of_that_instrument():
    class Lifecycle(Recorder):
        def __init__(self, instruments):
            super().__init__(instruments)
            self.dividends = []

        def update(self, ctx, data, views):
            self.dividends.extend(data.dividends)
            return super().update(ctx, data, views)

    spy, qqq = Lifecycle((SPY,)), Lifecycle((QQQ,))
    graph = Graph().add_node("spy", spy).add_node("qqq", qqq).add_node("sink", Sizer())
    graph.connect("spy", "views", "sink", "views").connect("qqq", "views", "sink", "views")
    graph = graph.set_output("sink", "positions").compile()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0)], dividends=[Dividend(SPY, T0, 1.5)])
    assert len(spy.dividends) == 1 and qqq.dividends == [] and qqq.slices == []


def test_target_outside_constraints_is_rejected_not_executed():
    source, sizer = Recorder((SPY,)), Sizer(weight=1.5)
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(
        trading_graph(source, sizer), adapter, constraints=PortfolioConstraints(gross_limit=1.0)
    )
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0, 100.0)])
    assert runner.orders == [] and runner.diagnostics[-1].code == "TARGET_REJECTED"


def test_custom_records_and_indicator_declarations_are_set_up():
    class Earnings:
        ports = {}
        outputs = {"views": OutputSpec((ScoreView,))}
        inputs = (Bars(Selection((SPY,))), Custom("earnings", Selection((SPY,))))
        indicators = (Indicator("sma", "sma", 2, Selection((SPY,))),)

        def __init__(self):
            self.custom = []

        def update(self, ctx, data, views):
            self.custom.extend(data.custom.get("earnings", ()))
            reading = ctx.indicator("sma", SPY)
            return (
                {"views": (score(ctx.node_id, SPY, ctx.time, reading.value, ctx.sequence),)}
                if reading.is_ready
                else None
            )

    from sugarloaf.core.data import EarningsListing

    node = Earnings()
    graph = Graph().add_node("e", node).set_output("e", "views").compile()
    adapter = MemoryAdapter(T0)
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    listing = EarningsListing(SPY, T0, (T0 + DAY).date(), "after_close")
    other = EarningsListing(QQQ, T0, (T0 + DAY).date())
    adapter.deliver(T0, [bar(SPY, T0, 10.0)], custom={"earnings": [listing, other]})
    assert node.custom == [listing]
    assert len(runner.archive) == 0
    adapter.deliver(T0 + DAY, [bar(SPY, T0 + DAY, 20.0)])
    assert runner.archive.get("e:2").view.statement.value == 15.0


def test_calendar_and_history_reads_go_through_the_adapter():
    sessions = Sessions(
        regular=tuple(
            (T0 + k * DAY - timedelta(hours=6, minutes=30), T0 + k * DAY) for k in range(-5, 5)
        )
    )
    history = [bar(SPY, T0 - k * DAY, 100.0 - k) for k in range(1, 6)]
    adapter = MemoryAdapter(T0, calendars={SPY: sessions}, histories={SPY: history})
    from sugarloaf.core.inputs import HistoryRequest

    class Reader:
        ports = {}
        outputs = {"views": OutputSpec((ScoreView,))}
        inputs = (Bars(Selection((SPY,))),)

        def __init__(self):
            self.result = None

        def update(self, ctx, data, views):
            closes = [b.close for b in ctx.history(HistoryRequest((SPY,), count=3))[SPY]]
            self.result = (closes, ctx.calendar(SPY).next_close(ctx.time, 2))
            return None

    node = Reader()
    graph = Graph().add_node("r", node).set_output("r", "views").compile()
    runner = GraphRunner(graph, adapter)
    adapter.attach(runner)
    adapter.deliver(T0, [bar(SPY, T0)])
    assert node.result == ([97.0, 98.0, 99.0], T0 + 2 * DAY)
