"""Barebones signal boundaries using supplied native-indicator observations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import NodeInputs
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.nodes.simple import PositiveSignalPositions
from sugarloaf.nodes.technical import EMASpread, RSIReversion, SMATrend

A = InstrumentId("SPY")
NOW = datetime(2025, 1, 3, 21, tzinfo=UTC)  # Friday's 16:00 New York close.
NEXT_CLOSE = datetime(2025, 1, 6, 21, tzinfo=UTC)  # Monday's close, one session later.


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
        # A stand-in exchange calendar: whatever is asked, the next regular close
        # is Monday's.
        instruments=NS(session_close=lambda instrument, after, count=1: NEXT_CLOSE),
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
    # The default horizon is one trading session, so a Friday-close score targets
    # and expires at Monday's close rather than on Saturday.
    assert views[0].metadata.expires_at == NEXT_CLOSE
    assert views[0].target.time.end == NEXT_CLOSE


def test_calendar_horizon_is_still_available_and_needs_no_calendar():
    node = EMASpread(A, fast=2, slow=3, horizon=timedelta(days=1))
    # Only a session horizon declares calendar access.
    assert len(node.requirements) == 2
    assert len(EMASpread(A, fast=2, slow=3).requirements) == 3
    views = node.evaluate(NodeInputs({}), context({("ema", 2): 110, ("ema", 3): 100}))
    assert views.by_port["views"][0].metadata.expires_at == NOW + timedelta(days=1)
    for invalid in (0, -1, True, 1.5, timedelta(0)):
        with pytest.raises(ContractError, match="[Hh]orizon"):
            EMASpread(A, fast=2, slow=3, horizon=invalid)


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
