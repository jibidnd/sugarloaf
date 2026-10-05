"""Teaching EMA forecasts retain their definition and align native observations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from examples.ema import EMAConfig, EMAScore
from sugarloaf.core.errors import NotReady
from sugarloaf.core.node import NodeInputs
from sugarloaf.core.types import InstrumentId
from sugarloaf.nodes.simple import MeanViewAggregator

NOW = datetime(2024, 1, 2, 15, tzinfo=UTC)
A = InstrumentId("A")


def context(points):
    def indicator(requirement, instrument):
        return SimpleNamespace(points=(points[dict(requirement.parameters)["period"]],))

    return SimpleNamespace(
        now=NOW,
        node_id="signal",
        snapshot_id=1,
        market=SimpleNamespace(indicator=indicator),
    )


def point(value, at=NOW):
    return SimpleNamespace(time=at, values={"value": value})


def test_distinct_ema_definitions_remain_separate_when_aggregated():
    ctx = context({2: point(110), 3: point(100), 4: point(95)})
    first = EMAScore(A, EMAConfig(fast=2, slow=3)).evaluate(NodeInputs({}), ctx)
    ctx.node_id = "other"
    second = EMAScore(A, EMAConfig(fast=2, slow=4)).evaluate(NodeInputs({}), ctx)
    ctx.node_id = "mean"
    combined = MeanViewAggregator().evaluate(
        NodeInputs({"views": first.by_port["views"] + second.by_port["views"]}), ctx
    )
    assert len(combined.by_port["views"]) == 2
    assert {v.statement.value for v in combined.by_port["views"]} == {
        110 / 100 - 1, 110 / 95 - 1
    }


@pytest.mark.parametrize(
    "fast,slow",
    [
        (point(110, NOW - timedelta(minutes=1)), point(100)),
        (point(110, NOW + timedelta(minutes=1)), point(100, NOW + timedelta(minutes=1))),
        (point(110), point(0)),
    ],
)
def test_ema_example_withdraws_when_native_points_are_unaligned_or_invalid(fast, slow):
    node = EMAScore(A, EMAConfig(fast=2, slow=3))
    with pytest.raises(NotReady, match="aligned admitted points"):
        node.evaluate(NodeInputs({}), context({2: fast, 3: slow}))
