"""The QuantConnect adapter over a fake LEAN: each declaration maps to the right call."""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from sugarloaf.adapters.quantconnect.adapter import QuantConnectAdapter, Ticker
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import (
    Bars,
    Custom,
    HistoryRequest,
    Indicator,
    LiquidityUniverse,
    Schedule,
    Selection,
    WarmUp,
)
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import PositionView, Score, ScoreView, return_target
from sugarloaf.execution.contracts import ExecutionPolicy
from sugarloaf.runtime import GraphRunner
from tests.fake_lean import LEAN, FakeAlgorithm, Slice, Symbol, daily_bar, minute_bar

SPY, QQQ, AAA = InstrumentId("USA:SPY"), InstrumentId("USA:QQQ"), InstrumentId("USA:AAA")
TICKERS = {SPY: "SPY", QQQ: "QQQ", AAA: Ticker("AAA")}
DAY = timedelta(days=1)


def score(node_id, instrument, time, value, sequence):
    return ScoreView(
        return_target(instrument, Interval(time, time + DAY)),
        Score(value, "test"),
        Metadata(f"{node_id}:{sequence}", node_id, time, time + DAY),
    )


class Source:
    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, *inputs, indicators=(), schedules=(), warm_up=None):
        self.inputs, self.indicators, self.schedules = inputs, indicators, schedules
        self.warm_up = warm_up
        self.slices, self.changes, self.scheduled = [], [], []
        self.reads = {}

    def on_securities_changed(self, ctx, added, removed):
        self.changes.append((added, removed))

    def update(self, ctx, data, views):
        self.slices.append(data)
        for name in [i.name for i in self.indicators]:
            for instrument in data.bars:
                self.reads[name, instrument] = ctx.indicator(name, instrument)
        return {
            "views": tuple(
                score(ctx.node_id, i, ctx.time, b.close, f"{ctx.sequence}:{i.value}")
                for i, b in data.bars.items()
            )
        }

    def on_scheduled(self, ctx, name, views):
        self.scheduled.append((name, ctx.time))
        return None


class Sizer:
    ports = {"views": __import__("sugarloaf.core.node", fromlist=["InputSpec"]).InputSpec(
        (ScoreView,), min_count=0)}
    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def update(self, ctx, data, views):
        positive = [v.target.subject for v in views["views"] if v.statement.value > 0]
        if not positive:
            return None
        return {
            "positions": (
                PositionView(
                    {i: 0.5 for i in positive}, "weight", "t",
                    Metadata(
                        f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, ctx.time + DAY
                    ),
                ),
            )
        }


def build(source, *, trading=False, algorithm=None, policy=None):
    algorithm = algorithm or FakeAlgorithm()
    graph = Graph().add_node("source", source)
    if trading:
        graph.add_node("sizer", Sizer()).connect("source", "views", "sizer", "views")
        compiled = graph.set_output("sizer", "positions").compile()
    else:
        compiled = graph.set_output("source", "views").compile()
    adapter = QuantConnectAdapter(algorithm, TICKERS, lean=LEAN)
    runner = GraphRunner(compiled, adapter, policy=policy or ExecutionPolicy(rebalance_band=0))
    adapter.runner = runner
    algorithm.adapter = adapter
    runner.start()
    return algorithm, adapter, runner


def test_daily_bars_subscribe_and_arrive_in_utc_with_a_native_window():
    source = Source(Bars(Selection((SPY,)), "daily", window=3))
    algorithm, adapter, runner = build(source)
    spy = adapter.symbols[SPY]
    assert spy.value == "SPY" and algorithm.securities[spy].resolution == LEAN.Resolution.DAILY
    settings = algorithm.securities[spy].settings
    assert settings["data_normalization_mode"] == LEAN.DataNormalizationMode.ADJUSTED
    for k, close in enumerate((10.0, 11.0, 12.0, 13.0)):
        day = date(2025, 1, 6) + k * DAY
        algorithm.push(datetime.combine(day, datetime.min.time()).replace(hour=16),
                       Slice([daily_bar(spy, day, close)]))
    bar = source.slices[-1].bars[SPY]
    # 16:00 New York in January is 21:00 UTC.
    assert bar.end == datetime(2025, 1, 9, 21, tzinfo=UTC) and bar.close == 13.0
    window = adapter.window(source.bars if hasattr(source, "bars") else source.inputs[0], SPY)
    assert len(window) == 3 and window[0].close == 13.0
    assert window.column("close") == (11.0, 12.0, 13.0)
    assert runner.archive.get("source:4:USA:SPY").view.statement.value == 13.0


def test_interval_bars_come_from_a_consolidator_and_indicators_from_helpers():
    five = Bars(Selection((SPY,)), "minute", timedelta(minutes=5), window=2)
    ema = Indicator("ema", "ema", 3, Selection((SPY,)), "minute", timedelta(minutes=5))
    source = Source(five, indicators=(ema,))
    algorithm, adapter, runner = build(source)
    spy = adapter.symbols[SPY]
    assert len(algorithm.consolidators[spy]) == 1
    (symbol, indicator), = algorithm.indicators
    assert symbol == spy and indicator.kind == "ema" and indicator.cadence == timedelta(minutes=5)
    assert algorithm.settings.automatic_indicator_warm_up is True
    start = datetime(2025, 1, 6, 9, 30)
    for k in range(11):
        algorithm.push(start + (k + 1) * timedelta(minutes=1),
                       Slice([minute_bar(spy, start + k * timedelta(minutes=1), 100.0 + k)]))
    # Two five-minute bars completed (at 09:35 and 09:40); each produced one slice.
    assert [s.bars[SPY].end for s in source.slices] == [
        datetime(2025, 1, 6, 14, 35, tzinfo=UTC), datetime(2025, 1, 6, 14, 40, tzinfo=UTC)
    ]
    assert source.slices[-1].bars[SPY].open == 105.0 and source.slices[-1].bars[SPY].volume == 500.0
    assert len(adapter.window(five, SPY)) == 2


def test_universe_members_get_windows_warmed_from_history_and_notify():
    history = {
        "AAA": [daily_bar(Symbol("AAA"), date(2024, 12, 30) + k * DAY, 50.0 + k) for k in range(5)],
    }
    algorithm = FakeAlgorithm(start=datetime(2025, 1, 6, 0, 0), history=history)
    liquid = LiquidityUniverse("liquid", 1, cadence="monthly")
    members = Bars(Selection(universe="liquid"), "daily", window=3)
    source = Source(liquid, members)
    algorithm, adapter, runner = build(source, algorithm=algorithm)
    (universe,) = algorithm.universes
    kind, selector, settings = universe
    assert kind == "fundamental" and settings.schedule.rules == [("month_start", ())]
    aaa, bbb = Symbol("AAA"), Symbol("BBB")
    rows = [
        SimpleNamespace(symbol=aaa, price=50.0, volume=1e6, dollar_volume=5e7,
                        has_fundamental_data=True),
        SimpleNamespace(symbol=bbb, price=5.0, volume=1e5, dollar_volume=5e5,
                        has_fundamental_data=True),
    ]
    assert selector(rows) == [aaa]
    algorithm.add_security(aaa, LEAN.Resolution.DAILY)
    algorithm.changed(added=(aaa,))
    instrument = adapter.bind(aaa)
    assert adapter.members("liquid") == (instrument,)
    assert source.changes == [((instrument,), ())]
    window = adapter.window(members, instrument)
    # Three of the five history bars ending before the clock fill the window.
    assert [b.close for b in window] == [54.0, 53.0, 52.0]
    # A bar for the member now reaches the node through the universe selection.
    algorithm.push(datetime(2025, 1, 6, 16), Slice([daily_bar(aaa, date(2025, 1, 6), 60.0)]))
    assert list(source.slices[-1].bars) == [instrument]
    # Dropping the member removes its window and tells the node.
    adapter._selected["liquid"] = ()
    algorithm.changed(removed=(aaa,))
    assert adapter.members("liquid") == () and source.changes[-1] == ((), (instrument,))


def test_targets_become_tagged_market_orders_and_feedback_is_forwarded():
    source = Source(Bars(Selection((SPY, QQQ)), "daily"))
    algorithm, adapter, runner = build(source, trading=True)
    spy, qqq = adapter.symbols[SPY], adapter.symbols[QQQ]
    algorithm.push(datetime(2025, 1, 6, 16), Slice([daily_bar(spy, date(2025, 1, 6), 100.0),
                                                     daily_bar(qqq, date(2025, 1, 6), -1.0 + 2)]))
    orders = {o.instrument: o for o in runner.orders}
    assert orders[SPY].quantity == 500 and orders[SPY].status == "submitted"
    assert algorithm.transactions.orders[1].tag == "sizer:1"
    assert algorithm.securities[spy].holdings.quantity == 500
    update = runner.updates[-1]
    assert update.status == "filled" and update.filled == 500 and update.tag == "sizer:1"
    # A refused order is recorded, nothing pending.
    algorithm.securities[spy].price = 0.001
    algorithm.push(datetime(2025, 1, 7, 16), Slice([daily_bar(spy, date(2025, 1, 7), 0.001)]))
    assert runner.orders[-1].status == "rejected" and "TooBig" in runner.orders[-1].reason


def test_new_target_cancels_open_orders_of_the_old_one():
    algorithm = FakeAlgorithm(fills=False)
    source = Source(Bars(Selection((SPY,)), "daily"))
    algorithm, adapter, runner = build(source, trading=True, algorithm=algorithm)
    spy = adapter.symbols[SPY]
    algorithm.push(datetime(2025, 1, 6, 16), Slice([daily_bar(spy, date(2025, 1, 6), 100.0)]))
    assert len(algorithm.transactions.get_open_orders()) == 1
    algorithm.push(datetime(2025, 1, 7, 16), Slice([daily_bar(spy, date(2025, 1, 7), 200.0)]))
    # The first order was canceled before the second target was sized from holdings alone.
    assert algorithm.transactions.canceled == [1]
    assert [o.quantity for o in runner.orders] == [500, 250]


def test_schedules_map_to_date_and_time_rules_and_fire_the_runner():
    schedule = Schedule("ten", calendar=SPY, after_open=10)
    source = Source(Bars(Selection((SPY,)), "daily"), schedules=(schedule,))
    algorithm, adapter, runner = build(source)
    (date_rule, time_rule, fn), = algorithm.scheduled
    spy = adapter.symbols[SPY]
    assert date_rule == ("every_day", (spy,)) and time_rule == ("after_open", spy, 10)
    algorithm.time = datetime(2025, 1, 6, 9, 40)
    fn()
    assert source.scheduled == [("ten", datetime(2025, 1, 6, 14, 40, tzinfo=UTC))]


def test_warm_up_history_calendar_and_portfolio_reads():
    history = {"SPY": [daily_bar(Symbol("SPY"), date(2024, 12, 30) + k * DAY, 10.0 + k)
                       for k in range(5)]}
    algorithm = FakeAlgorithm(start=datetime(2025, 1, 6, 16, 0), history=history)
    source = Source(Bars(Selection((SPY,)), "daily"), warm_up=WarmUp(count=10))
    algorithm, adapter, runner = build(source, algorithm=algorithm)
    assert algorithm.warm_up_request == (10, LEAN.Resolution.DAILY)
    bars = adapter.history(HistoryRequest((SPY,), count=2))[SPY]
    assert [b.close for b in bars] == [13.0, 14.0]
    calendar = adapter.calendar(SPY)
    friday_close = datetime(2025, 1, 3, 21, tzinfo=UTC)
    assert calendar.next_close(friday_close) == datetime(2025, 1, 6, 21, tzinfo=UTC)
    assert calendar.next_open(friday_close) == datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
    assert not calendar.is_open(friday_close)
    spy = adapter.symbols[SPY]
    algorithm.securities[spy].price = 20.0
    algorithm.securities[spy].holdings.quantity = 10.0
    portfolio = adapter.portfolio()
    assert portfolio.holdings == {SPY: 10.0} and portfolio.prices[SPY] == 20.0
    assert portfolio.nav == 100_000 + 200 and portfolio.weight(SPY) == pytest.approx(200 / 100_200)


def test_lifecycle_and_earnings_records_reach_the_node():
    source = Source(Bars(Selection((SPY,)), "daily"), Custom("earnings", Selection((SPY,))))
    algorithm, adapter, runner = build(source)
    assert algorithm.custom_data == [adapter._earnings_type]
    spy = adapter.symbols[SPY]
    listing = SimpleNamespace(symbol=spy, report_date=date(2025, 1, 30), report_time="After Market")
    other = SimpleNamespace(symbol=Symbol("ZZZ"), report_date=date(2025, 1, 30), report_time="x")
    slice = Slice(
        [daily_bar(spy, date(2025, 1, 6), 100.0)],
        custom={adapter._earnings_type: {spy: listing, other.symbol: other}},
        dividends={spy: SimpleNamespace(distribution=1.5, reference_price=100.0)},
        splits={spy: SimpleNamespace(split_factor=0.5, type="SplitOccurred")},
    )
    algorithm.push(datetime(2025, 1, 6, 16), slice)
    data = source.slices[-1]
    (earnings,) = data.custom["earnings"]
    assert earnings.instrument == SPY and earnings.timing == "after_close"
    assert data.dividends[0].amount == 1.5 and data.splits[0].factor == 0.5


def test_launcher_saves_results_and_forecasts_to_the_object_store():
    from sugarloaf.adapters.quantconnect import launcher
    from sugarloaf.deployment import Deployment

    algorithm = FakeAlgorithm()
    source = Source(Bars(Selection((SPY,)), "daily"))
    graph = Graph().add_node("source", source).set_output("source", "views").compile()
    deployment = Deployment("demo", {}, lambda: graph, TICKERS)
    import sys

    sys.modules.setdefault("AlgorithmImports", SimpleNamespace(
        AccountType=SimpleNamespace(MARGIN="margin"),
        BrokerageName=SimpleNamespace(DEFAULT="default"),
    ))
    for name in ("set_start_date", "set_end_date", "set_time_zone", "set_account_currency",
                 "set_cash", "set_brokerage_model", "set_benchmark"):
        setattr(algorithm, name, lambda *a, **k: None)
    original = launcher.QuantConnectAdapter
    launcher.QuantConnectAdapter = lambda *a, **k: original(*a, lean=LEAN, **k)
    try:
        adapter = launcher.start(algorithm, deployment)
    finally:
        launcher.QuantConnectAdapter = original
    algorithm.adapter = adapter
    spy = adapter.symbols[SPY]
    algorithm.push(datetime(2025, 1, 6, 16), Slice([daily_bar(spy, date(2025, 1, 6), 100.0)]))
    launcher.finish(algorithm, deployment, adapter)
    result = json.loads(algorithm.saved["sugarloaf/results/1/run/result.json"])
    assert result["mode"] == "forecast" and result["run"]["forecasts"] == 1
    forecasts = json.loads(algorithm.saved["sugarloaf/results/1/run/forecasts.json"])
    assert forecasts[0]["node_id"] == "source"
    assert algorithm.messages[0].startswith("SUGARLOAF_RUN ")
    assert "SUGARLOAF_RESULT" in algorithm.messages[-1]


def test_explicit_windows_are_warmed_from_history_at_subscription():
    history = {"SPY": [daily_bar(Symbol("SPY"), date(2024, 12, 30) + k * DAY, 10.0 + k)
                       for k in range(5)]}
    algorithm = FakeAlgorithm(start=datetime(2025, 1, 6, 0, 0), history=history)
    bars = Bars(Selection((SPY,)), "daily", window=3)
    source = Source(bars)
    algorithm, adapter, runner = build(source, algorithm=algorithm)
    # Before any slice, the window holds the three newest history bars.
    assert [b.close for b in adapter.window(bars, SPY)] == [14.0, 13.0, 12.0]


def test_daily_declaration_on_a_minute_security_is_consolidated_from_minutes():
    minute = Bars(Selection((SPY,)), "minute", window=2)
    daily = Bars(Selection((SPY,)), "daily", window=2)

    class Both(Source):
        pass

    source = Both(daily, minute)
    algorithm, adapter, runner = build(source)
    spy = adapter.symbols[SPY]
    # The security was subscribed at minute (finest first), and the daily declaration
    # consolidates that feed over the session instead of taking minute bars.
    assert algorithm.securities[spy].resolution == LEAN.Resolution.MINUTE
    assert SPY in adapter.consolidators[daily.key]
    start = datetime(2025, 1, 6, 9, 30)
    for k in range(390):
        algorithm.push(start + (k + 1) * timedelta(minutes=1),
                       Slice([minute_bar(spy, start + k * timedelta(minutes=1), 100.0 + k)]))
    # Next session's first minute completes the daily bar.
    nxt = datetime(2025, 1, 7, 9, 30)
    algorithm.push(nxt + timedelta(minutes=1), Slice([minute_bar(spy, nxt, 50.0)]))
    window = adapter.window(daily, SPY)
    assert len(window) == 1 and window[0].open == 100.0 and window[0].close == 489.0
    assert window[0].end == datetime(2025, 1, 6, 21, tzinfo=UTC)
    assert len(adapter.window(minute, SPY)) == 2
