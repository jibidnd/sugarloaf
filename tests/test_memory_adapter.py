"""The memory adapter as a platform: windows, consolidation, indicators, orders."""

from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryAdapter
from sugarloaf.core.data import Bar
from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.inputs import Bars, Indicator, Selection
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import ExecutionPolicy, desired_quantity, order_quantity

SPY = InstrumentId("SPY")
T0 = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
MINUTE, DAY = timedelta(minutes=1), timedelta(days=1)


def minute_bar(end, close, volume=10.0):
    return Bar(SPY, end - MINUTE, end, close, close + 1, close - 1, close, volume)


def test_consolidated_window_and_indicator_follow_the_interval():
    adapter = MemoryAdapter(T0)
    five = Bars(Selection((SPY,)), "minute", timedelta(minutes=5), window=2)
    adapter.subscribe(five, (SPY,))
    sma = Indicator("sma", "sma", 2, Selection((SPY,)), "minute", timedelta(minutes=5))
    adapter.add_indicator(sma, (SPY,))
    for k in range(11):
        adapter.deliver(T0 + (k + 1) * MINUTE, [minute_bar(T0 + (k + 1) * MINUTE, 100.0 + k)])
    window = adapter.window(five, SPY)
    # Two five-minute bars completed; the newest is first and spans minutes 6-10.
    assert len(window) == 2
    assert window[0].start == T0 + 5 * MINUTE and window[0].end == T0 + 10 * MINUTE
    assert window[0].open == 105.0 and window[0].close == 109.0 and window[0].volume == 50.0
    assert window.column("close") == (104.0, 109.0)
    reading = adapter.indicator(sma, SPY)
    assert reading.is_ready and reading.value == pytest.approx((104.0 + 109.0) / 2)


def test_window_is_bounded_by_the_largest_declared_window():
    adapter = MemoryAdapter(T0)
    small = Bars(Selection((SPY,)), "minute", window=1)
    large = Bars(Selection((SPY,)), "minute", window=3)
    adapter.subscribe(small, (SPY,))
    adapter.subscribe(large, (SPY,))
    for k in range(5):
        adapter.deliver(T0 + (k + 1) * MINUTE, [minute_bar(T0 + (k + 1) * MINUTE, k)])
    assert [b.close for b in adapter.window(large, SPY)] == [4.0, 3.0, 2.0]
    assert [b.close for b in adapter.window(small, SPY)] == [4.0]
    with pytest.raises(CapabilityError):
        adapter.window(Bars(Selection((SPY,)), "daily"), SPY)


def test_unmatched_resolution_bars_are_ignored():
    adapter = MemoryAdapter(T0)
    daily = Bars(Selection((SPY,)), "daily")
    adapter.subscribe(daily, (SPY,))
    adapter.deliver(T0 + MINUTE, [minute_bar(T0 + MINUTE, 1.0)])
    assert len(adapter.window(daily, SPY)) == 0


def test_clock_does_not_run_backwards():
    adapter = MemoryAdapter(T0)
    with pytest.raises(ContractError):
        adapter.deliver(T0 - MINUTE)


def test_sizing_rules():
    whole, exact = desired_quantity(0.2, "weight", 100.5, 10010.0)
    assert (whole, exact) == (19.0, pytest.approx(19.92, abs=0.01))
    policy = ExecutionPolicy(min_trade_value=1000, rebalance_band=0.0025)
    # Inside the band: no order. Far outside: the whole-lot residual.
    assert order_quantity(19, 19.92, 20, 0, 100.5, 10010, "weight", policy) == 0
    assert order_quantity(100, 100.4, 0, 0, 100.5, 100000, "weight", policy) == 100
    # Pending orders count; an exit to zero always goes.
    assert order_quantity(100, 100.4, 0, 40, 100.5, 100000, "weight", policy) == 60
    assert order_quantity(0, 0, 3, 0, 100.5, 100000, "weight", policy) == -3
    # Quantity targets are exact instructions but still respect the value floor.
    assert order_quantity(5, 5, 0, 0, 100.5, 100000, "quantity", policy) == 0
    assert order_quantity(50, 50, 0, 0, 100.5, 100000, "quantity", policy) == 50


def test_execute_fills_immediately_and_cancels_old_orders_on_a_new_target():
    adapter = MemoryAdapter(T0, fills=False)
    adapter.set_price(SPY, 100.0)
    policy = ExecutionPolicy(rebalance_band=0)

    def target(weight, identity):
        return PositionView({SPY: weight}, "weight", "t", Metadata(identity, "n", T0, T0 + DAY))

    (first,) = adapter.execute(target(0.1, "a"), policy)
    assert first.quantity == 100 and len(adapter.open_orders) == 1
    # Same target again: the open order already covers it.
    assert adapter.execute(target(0.1, "a"), policy) == ()
    # A new target cancels the open order and sizes from holdings alone.
    (second,) = adapter.execute(target(0.2, "b"), policy)
    assert second.quantity == 200 and [o.tag for o in adapter.open_orders] == ["b"]
    adapter.fill(second.order_id)
    assert adapter.holdings[SPY] == 200 and adapter.cash == 100000 - 20000
