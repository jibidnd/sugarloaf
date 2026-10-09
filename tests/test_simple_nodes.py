from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import NodeInputs
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Probability,
    ProbabilityView,
    ReturnEvent,
    ReturnView,
    Score,
    ScoreView,
)
from sugarloaf.nodes.simple import MeanViewAggregator, PositiveSignalPositions

A, B = InstrumentId("A"), InstrumentId("B")
NOW = datetime(2025, 1, 1, tzinfo=UTC)
END = NOW + timedelta(days=5)
CTX = SimpleNamespace(node_id="baseline", snapshot_id=1, now=NOW)


def forecast(identity, instrument=A, value=0.1, expires=END):
    return ReturnView.create(
        instrument, Interval(NOW, END), value, 0.03, Metadata(identity, identity, NOW, expires)
    )


def evaluate(node, *views):
    return node.evaluate(NodeInputs({"views": views}), CTX).by_port


def test_mean_preserves_targets_lineage_and_earliest_expiry_without_confidence_shrinkage():
    first = forecast("first", value=0.1, expires=NOW + timedelta(hours=1))
    second = forecast("second", value=-0.2)
    second = replace(second, statement=replace(second.statement, forecast_error_scale=0.06))
    other_horizon = replace(
        forecast("later"), target=replace(first.target, time=Interval(NOW, END + timedelta(days=1)))
    )
    outputs = evaluate(MeanViewAggregator(), first, second, other_horizon)["views"]
    assert len(outputs) == 2
    assert outputs[0].statement.value == pytest.approx(-0.05)
    assert outputs[0].statement.forecast_error_scale == 0.06
    assert outputs[0].metadata.parents == ("first", "second")
    assert outputs[0].metadata.expires_at == first.metadata.expires_at
    with pytest.raises(ContractError, match="Duplicate"):
        evaluate(MeanViewAggregator(), first, first)


def test_score_scales_stay_separate_and_probability_means_keep_event_meaning():
    base = forecast("first")
    score = ScoreView(base.target, Score(2, "scale-a"), base.metadata)
    second = replace(score, statement=Score(-1, "scale-b"), metadata=forecast("second").metadata)
    assert len(evaluate(MeanViewAggregator(), score, second)["views"]) == 2
    p = ProbabilityView(base.target, Probability((ReturnEvent(lower=0),), (0.6,)), base.metadata)
    q = replace(
        p,
        statement=replace(p.statement, probabilities=(0.8,)),
        metadata=forecast("second").metadata,
    )
    combined = evaluate(MeanViewAggregator(), p, q)["views"][0]
    assert combined.statement.probabilities == pytest.approx((0.7,))


def test_positions_equal_weight_positives_exit_negatives_and_hold_on_missing_evidence():
    node = PositiveSignalPositions((A, B), budget=0.8)
    a, b = forecast("a"), forecast("b", B)
    assert dict(evaluate(node, a, b)["positions"][0].positions) == {A: 0.4, B: 0.4}
    negative = replace(a, statement=replace(a.statement, value=-0.1))
    assert dict(evaluate(node, negative, b)["positions"][0].positions) == {A: 0, B: 0.8}
    zero = replace(b, statement=replace(b.statement, value=0))
    assert dict(evaluate(node, negative, zero)["positions"][0].positions) == {A: 0, B: 0}
    with pytest.raises(NotReady, match="Missing"):
        evaluate(node, a)
    with pytest.raises(ContractError, match="one claim"):
        evaluate(node, a, forecast("another"), b)


def test_position_conversion_rejects_other_probability_events_and_mixed_horizons():
    node = PositiveSignalPositions((A,))
    a = forecast("a")
    p = ProbabilityView(a.target, Probability((ReturnEvent(lower=0),), (0.5,)), a.metadata)
    assert evaluate(node, p)["positions"][0].positions[A] == 0
    p = replace(p, statement=replace(p.statement, probabilities=(0.6,)))
    assert evaluate(node, p)["positions"][0].positions[A] == 0.9
    bad = replace(p, statement=Probability((ReturnEvent(upper=0),), (0.6,)))
    with pytest.raises(ContractError, match="explicit return"):
        evaluate(node, bad)
    b = forecast("b", B)
    b = replace(b, target=replace(b.target, time=Interval(NOW, END + timedelta(days=1))))
    with pytest.raises(ContractError, match="share a horizon"):
        evaluate(PositiveSignalPositions((A, B)), a, b)
    expired = replace(a, metadata=Metadata("old", "old", NOW - timedelta(days=2), NOW))
    with pytest.raises(NotReady, match="expired"):
        evaluate(node, expired)
