"""Barebones signal boundaries using supplied native-indicator observations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import NodeInputs
from sugarloaf.core.types import InstrumentId
from sugarloaf.nodes.simple import PositiveSignalPositions
from sugarloaf.nodes.technical import EMASpread, RSIReversion, SMATrend

A = InstrumentId("SPY")
NOW = datetime(2025, 1, 2, 21, tzinfo=UTC)


def context(values, *, bar_close=110, time=NOW):
    def indicator(requirement, instrument):
        key = (requirement.kind, dict(requirement.parameters)["period"])
        value = values[key]
        point = value if hasattr(value, "time") else NS(time=time, values={"value": value})
        return NS(points=(point,))

    return NS(
        now=NOW,
        node_id="signal",
        snapshot_id=1,
        market=NS(indicator=indicator, bars=lambda *args: (NS(end=time, close=bar_close),)),
    )


@pytest.mark.parametrize(
    "node,values,positive",
    [
        (EMASpread(A, fast=2, slow=3), {("ema", 2): 110, ("ema", 3): 100}, True),
        (RSIReversion(A), {("rsi", 14): 20}, True),
        (RSIReversion(A), {("rsi", 14): 30}, False),
        (SMATrend(A), {("sma", 210): 120}, False),
    ],
)
def test_signal_to_naive_positions_without_return_calibration(node, values, positive):
    ctx = context(values)
    views = node.evaluate(NodeInputs({}), ctx).by_port["views"]
    ctx.node_id = "positions"
    result = PositiveSignalPositions((A,), budget=0.8).evaluate(NodeInputs({"views": views}), ctx)
    assert result.by_port["positions"][0].positions[A] == (0.8 if positive else 0)
    assert views[0].metadata.expires_at == NOW + timedelta(days=1)


def test_indicator_clock_alignment_and_invalid_periods_fail_explicitly():
    node = EMASpread(A, fast=2, slow=3)
    old = NS(time=NOW - timedelta(days=1), values={"value": 100})
    with pytest.raises(NotReady, match="aligned"):
        node.evaluate(NodeInputs({}), context({("ema", 2): old, ("ema", 3): 100}))
    future = NS(time=NOW + timedelta(minutes=1), values={"value": 100})
    with pytest.raises(NotReady, match="data clock"):
        SMATrend(A).evaluate(NodeInputs({}), context({("sma", 210): future}))
    with pytest.raises(ContractError, match="positive integers"):
        SMATrend(A, period=True)
    with pytest.raises(ContractError, match="below slow"):
        EMASpread(A, fast=50, slow=20)
