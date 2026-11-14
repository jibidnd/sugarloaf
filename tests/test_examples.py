"""The shipped examples run on the memory adapter and build valid deployments."""

from datetime import UTC, datetime, timedelta

import pytest

from examples import memory
from examples.ema import EMAConfig, build_forecast_graph, build_graph
from sugarloaf.adapters.memory import MemoryAdapter, Sessions
from sugarloaf.core.data import Bar
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.execution import ExecutionPolicy
from sugarloaf.runtime import GraphRunner

SPY = InstrumentId("USA:SPY")
DAY = timedelta(days=1)
T0 = datetime(2025, 1, 6, 21, tzinfo=UTC)


def test_memory_example_nets_two_branches_into_one_order():
    assert memory.run() == {"holdings": 50.0, "orders": 1, "diagnostics": 1}


def _sessions():
    return Sessions(
        regular=tuple(
            (T0 + k * DAY - timedelta(hours=6, minutes=30), T0 + k * DAY) for k in range(-10, 10)
        )
    )


def _feed(adapter, closes):
    for k, close in enumerate(closes):
        end = T0 - (len(closes) - 1 - k) * DAY
        adapter.deliver(end, [Bar(SPY, end - DAY, end, close, close + 1, close - 1, close, 100.0)])


def test_ema_trading_graph_goes_long_on_a_rising_series():
    config = EMAConfig(fast=2, slow=3)
    adapter = MemoryAdapter(T0 - 5 * DAY, calendars={SPY: _sessions()})
    runner = GraphRunner(build_graph(SPY, config), adapter,
                         policy=ExecutionPolicy(rebalance_band=0))
    adapter.attach(runner)
    _feed(adapter, [10, 11, 12, 13])
    assert runner.last_target.positions[SPY] == config.weight
    # Bought at 12 when both averages were ready, then rebalanced to 20% of NAV at 13.
    nav = adapter.portfolio().nav
    assert adapter.holdings[SPY] == pytest.approx(nav * config.weight / 13, abs=1)


def test_ema_forecast_graph_archives_scores_and_trades_nothing():
    adapter = MemoryAdapter(T0 - 5 * DAY, calendars={SPY: _sessions()})
    runner = GraphRunner(build_forecast_graph(SPY, EMAConfig(fast=2, slow=3)), adapter)
    adapter.attach(runner)
    _feed(adapter, [10, 11, 12, 13])
    assert len(runner.archive) == 2 and runner.orders == []


def test_public_factories_build_deployments():
    from deployments.quantconnect.config import ema_definition, ema_forecasts
    from deployments.quantconnect.index_check import deployment

    parameter = lambda name: {"ticker": "SPY", "interval_minutes": "5"}.get(name)  # noqa: E731
    trading = ema_definition(parameter)
    assert trading.graph_factory().emits_positions()
    forecasts = ema_forecasts(parameter)
    assert not forecasts.graph_factory().emits_positions()
    probe = deployment(lambda name: None)
    assert not probe.graph_factory().emits_positions()
