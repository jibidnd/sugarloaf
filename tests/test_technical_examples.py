from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from examples.technical import DonchianBreakout, RSIMeanReversion
from examples.technical.indicators import ema, rsi
from examples.technical.memory import run
from sugarloaf.core.errors import NotReady
from sugarloaf.core.events import EventBatch
from sugarloaf.core.node import NodeInputs
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.services import Context
from sugarloaf.market.contracts import Bar

A = InstrumentId("fixture:A")
NOW = datetime(2025, 1, 6, 16, tzinfo=timezone.utc)


def evaluate(node, closes, sequence):
    now = NOW + timedelta(days=sequence)
    bars = tuple(
        Bar(
            A,
            now - timedelta(days=len(closes) - k),
            now - timedelta(days=len(closes) - k - 1),
            now,
            price,
            price,
            price,
            price,
            100,
        )
        for k, price in enumerate(closes)
    )
    market = SimpleNamespace(bars=lambda requirement, instrument: bars)
    ctx = Context(EventBatch(sequence, now), {"market": lambda: market}, node_id="signal")
    return node.evaluate(NodeInputs({}), ctx).by_port["positions"][0].positions[A]


def test_indicator_seeds_wilder_recursion_and_boundary_conventions():
    assert ema([1, 2, 3], 3) == 2
    assert ema([1, 2, 3, 4, 5], 3) == 4
    assert rsi([10, 11, 10, 12], 3) == pytest.approx(75)
    assert rsi([10, 11, 10, 12, 10], 3) == pytest.approx(300 / 7)
    assert rsi([10] * 8, 3) == 50
    assert rsi([10, 11, 12, 13], 3) == 100
    assert rsi([13, 12, 11, 10], 3) == 0
    with pytest.raises(NotReady):
        rsi([10, 11, 12], 3)


def test_rsi_holds_between_entry_and_exit_thresholds_and_emits_zero():
    node = RSIMeanReversion((A,), period=3, history=4, weight=0.2)
    assert evaluate(node, [13, 12, 11, 10], 1) == 0.2
    assert evaluate(node, [10, 9, 8, 9], 2) == 0.2  # RSI 33.3: hold, don't exit at 30.
    assert evaluate(node, [10, 11, 12, 13], 3) == 0


def test_donchian_excludes_current_bar_and_holds_until_lower_break():
    node = DonchianBreakout((A,), entry_period=3, exit_period=2, weight=0.2)
    assert evaluate(node, [10, 11, 12, 13], 1) == 0.2
    assert evaluate(node, [11, 12, 13, 12.5], 2) == 0.2
    assert evaluate(node, [12, 13, 12.5, 11], 3) == 0


@pytest.mark.parametrize("strategy", ["ema", "rsi", "donchian"])
def test_portable_strategy_targets_reach_fills_and_replay_is_idempotent(strategy):
    result = run(strategy)
    assert result["orders"] > 0
    assert {t["target_weight"] for t in result["transitions"]} == {0, 0.2}
    assert result["replay_added_orders"] == 0
    # The final decline exits trend followers but re-enters the contrarian RSI rule.
    expected_last = 0.2 if strategy == "rsi" else 0
    assert result["transitions"][-1]["target_weight"] == expected_last
    assert (result["final_quantity"] > 0) == (strategy == "rsi")
