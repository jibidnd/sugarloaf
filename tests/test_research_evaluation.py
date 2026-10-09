from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import log

import numpy as np
import pytest
from scipy.stats import multivariate_normal

from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.node import NodeOutputs
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Distribution,
    DistributionView,
    PositionView,
    Probability,
    ProbabilityView,
    RankingView,
    RelativeReturnView,
    ReturnEvent,
    ReturnView,
    Score,
    ScoreView,
    Target,
    return_target,
)
from sugarloaf.market.contracts import Observation
from sugarloaf.research import (
    ForecastArchive,
    Outcome,
    ReturnOutcomeResolver,
    default_metrics,
    evaluate,
)


@dataclass(frozen=True)
class GaussianReturns:
    """Test-only joint density; scoring depends on the law protocol, not a model."""

    mean: tuple
    covariance: tuple
    units: str = "decimal"
    support: tuple = (-float("inf"), float("inf"))

    @property
    def dimension(self):
        return len(self.mean)

    def logpdf(self, value):
        return float(multivariate_normal.logpdf(value, self.mean, self.covariance))


@dataclass(frozen=True)
class EventMasses:
    events: tuple
    masses: tuple
    units: str = "decimal"
    support: tuple = (-1, float("inf"))


A, B, C = (InstrumentId(name) for name in "ABC")
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=10)
INTERVAL = Interval(START, END)


def metadata(identity, at=START, expires=None):
    return Metadata(identity, "signal", at, expires or END + timedelta(days=1))


def archive_views(*views, graph_time=START, received_at=None):
    archive = ForecastArchive()
    archive.record(
        "signal",
        NodeOutputs({"forecasts": views}),
        EventBatch(1, graph_time, received_at=received_at),
    )
    return archive


def anchors(instrument, at, convention, as_of):
    assert convention in {"price_return", "total_return"}
    value = 100 if at == START else {A: 110, B: 105, C: 90}[instrument]
    return Observation(at, at, value)


def test_archive_preserves_replaced_expired_forecasts_and_first_emission_provenance():
    first = ReturnView.create(
        A, INTERVAL, 0.1, 0.03, metadata("first", expires=START + timedelta(days=1))
    )
    receipt = START + timedelta(seconds=2)
    archive = archive_views(first, received_at=receipt)
    archive.record(
        "signal", NodeOutputs({"forecasts": (first,)}), EventBatch(2, START + timedelta(hours=1))
    )
    revised = ReturnView.create(
        A, INTERVAL, 0.05, 0.03, metadata("revised", START + timedelta(days=1))
    )
    archive.record(
        "signal", NodeOutputs({"forecasts": (revised,)}), EventBatch(3, START + timedelta(days=1))
    )
    archive.record("signal", NodeOutputs({}), EventBatch(4, END + timedelta(days=2)))
    assert len(archive) == 2
    retained = archive.get("first")
    assert retained.view is first
    assert retained.batch_sequence == 1
    assert retained.graph_time == START
    assert retained.received_at == receipt
    assert retained.view.metadata.cutoff == START
    report = evaluate(archive, ReturnOutcomeResolver(anchors), as_of=END)
    assert report.coverage == {
        "issued": 2,
        "scored": 2,
        "by_status": {"scored": 2},
        "issued_after_target_end": 0,
    }
    assert report.rows[0].metrics["squared_error"] == pytest.approx(0)
    assert report.rows[1].metrics["squared_error"] == pytest.approx(0.0025)
    assert len(report.summary()) == 2  # Original and revised issue-to-target lead differ.


def test_archive_reused_identity_validation_is_atomic_and_actions_are_not_predictions():
    first = ReturnView.create(A, INTERVAL, 0.1, 0.03, metadata("first"))
    archive = archive_views(first)
    new = ReturnView.create(B, INTERVAL, 0.1, 0.03, metadata("new"))
    bad = replace(first, statement=replace(first.statement, value=0.9))
    with pytest.raises(ContractError, match="changed contents"):
        archive.record("signal", NodeOutputs({"forecasts": (new, bad)}), EventBatch(2, START))
    assert len(archive) == 1
    action = PositionView({A: 0.5}, "weight", "branch", metadata("position"))
    archive.record("signal", NodeOutputs({"positions": (action,)}), EventBatch(3, START))
    assert len(archive) == 1


def test_exact_outcomes_keep_conventions_joint_order_and_relative_difference():
    requests = []

    def tracked(instrument, at, convention, as_of):
        requests.append((instrument, at, convention, as_of))
        return anchors(instrument, at, convention, as_of)

    resolver = ReturnOutcomeResolver(tracked, provenance={"provider": "recorded-QC"})
    target = Target((B, A), "return", INTERVAL, convention="total_return")
    joint = resolver.resolve(target, as_of=END)
    assert joint.value == pytest.approx((0.05, 0.1))
    assert all(call[2] == "total_return" for call in requests)
    assert joint.provenance["provider"] == "recorded-QC"
    relative = RelativeReturnView.create(A, B, INTERVAL, 0.02, 0.01, metadata("relative"))
    outcome = resolver.resolve(relative.target, as_of=END)
    assert outcome.value == pytest.approx(0.05)
    metrics = default_metrics(relative, outcome)
    assert metrics["error"] == pytest.approx(-0.03)
    assert metrics["standardized_error"] == pytest.approx(-3)
    mismatched = replace(relative.target, convention="total_return")
    assert resolver.resolve(mismatched, as_of=END).status == "unsupported"


def test_pending_missing_delayed_and_wrong_time_anchors_never_become_zero_labels():
    calls = []

    def missing(*args):
        calls.append(args)
        return None

    target = return_target(A, INTERVAL)
    resolver = ReturnOutcomeResolver(missing)
    assert resolver.resolve(target, as_of=START).status == "pending"
    assert not calls
    assert resolver.resolve(target, as_of=END).status == "unavailable"
    delayed = ReturnOutcomeResolver(
        lambda i, at, c, cutoff: Observation(at, END + timedelta(days=1), 100)
    )
    assert delayed.resolve(target, as_of=END).status == "unavailable"
    substituted = ReturnOutcomeResolver(
        lambda i, at, c, cutoff: Observation(
            at + timedelta(minutes=1), at + timedelta(minutes=1), 100
        )
    )
    with pytest.raises(ContractError, match="substituted"):
        substituted.resolve(target, as_of=END)
    unsupported = replace(target, quantity="variance")
    assert resolver.resolve(unsupported, as_of=END).status == "unsupported"


def test_probability_rules_preserve_boundary_losses_calibration_and_bin_convention():
    target = return_target(A, INTERVAL)
    independent = ProbabilityView(
        target, Probability((ReturnEvent(lower=0.1),), (1.0,)), metadata("boundary")
    )
    # Exactly the upper/lower threshold is not in the lower-open positive event.
    outcome = Outcome(target, 0.1, END)
    metrics = default_metrics(independent, outcome)
    assert metrics["observed_0"] == 0
    assert metrics["brier"] == 1
    assert metrics["log_loss"] == float("inf")
    bins = ProbabilityView(
        target,
        Probability((ReturnEvent(upper=0), ReturnEvent(lower=0)), (0.2, 0.8), exhaustive=True),
        metadata("bins"),
    )
    scored = default_metrics(bins, outcome)
    assert scored["brier"] == pytest.approx(0.08)  # Categorical sum, not half sum.
    assert scored["log_loss"] == pytest.approx(-log(0.8))

    class Exact:
        def resolve(self, target, *, as_of):
            return Outcome(target, 0.1, END)

    report = evaluate(archive_views(independent), Exact(), as_of=END)
    summary = report.summary()[0]
    assert summary["means"]["log_loss"] == float("inf")
    assert summary["infinite_scores"] == 1
    assert summary["calibration"]["0"][0]["event_frequency"] == 0


def test_rankings_score_ties_and_group_same_universe_without_conditioning_on_predicted_order():
    first = RankingView.create(((A, B), (C,)), INTERVAL, 0.1, metadata("rank"))
    outcome = Outcome(first.target, (0.1, 0.1, -0.1), END)
    metrics = default_metrics(first, outcome)
    assert metrics["kendall_tau_b"] == pytest.approx(1)
    assert metrics["pair_concordance"] == 1
    assert metrics["within_tie_realized_spread"] == 0
    reversed_rank = RankingView.create(((C,), (A, B)), INTERVAL, 0.1, metadata("reverse"))
    report = evaluate(
        archive_views(first, reversed_rank), ReturnOutcomeResolver(anchors), as_of=END
    )
    assert len(report.summary()) == 1
    assert report.summary()[0]["count"] == 2


def test_distribution_log_score_retains_joint_dependence_and_discrete_support():
    target = Target((A, B), "return", INTERVAL)
    outcome = Outcome(target, (0.1, -0.05), END)
    independent = GaussianReturns((0, 0), ((0.04, 0), (0, 0.04)))
    correlated = replace(independent, covariance=((0.04, 0.03), (0.03, 0.04)))
    first = DistributionView(target, Distribution(independent), metadata("independent"))
    second = DistributionView(target, Distribution(correlated), metadata("correlated"))
    assert default_metrics(first, outcome)["log_score"] == pytest.approx(
        -independent.logpdf(outcome.value)
    )
    assert (
        default_metrics(first, outcome)["log_score"]
        != default_metrics(second, outcome)["log_score"]
    )
    scalar = return_target(A, INTERVAL)
    law = EventMasses((ReturnEvent(upper=0), ReturnEvent(lower=0)), (1, 0))
    bins = DistributionView(scalar, Distribution(law), metadata("discrete"))
    assert default_metrics(bins, Outcome(scalar, 0.1, END))["log_loss"] == float("inf")


def test_raw_scores_get_chronological_cohort_association_not_an_invented_return_error():
    archive = ForecastArchive()
    for k in range(4):
        issued = START + timedelta(days=k)
        target = return_target(A, Interval(issued, issued + timedelta(days=10)))
        view = ScoreView(target, Score(float(k), "signal-v1"), metadata(str(k), issued))
        archive.record("signal", NodeOutputs({"scores": (view,)}), EventBatch(k, issued))

    class Outcomes:
        def resolve(self, target, *, as_of):
            return Outcome(target, float((target.time.start - START).days) * 0.01, target.time.end)

    report = evaluate(archive, Outcomes(), as_of=END + timedelta(days=4))
    assert set(report.rows[0].metrics) == {"score", "realized"}
    summary = report.summary()[0]
    assert summary["pearson_correlation"] == pytest.approx(1)
    assert summary["spearman_correlation"] == pytest.approx(1)
    assert summary["distinct_targets"] == 4


def test_custom_outcomes_and_metrics_validate_target_and_availability_without_future_inputs():
    view = ReturnView.create(A, INTERVAL, 0.03, 0.02, metadata("return"))
    archive = archive_views(view)

    class Custom:
        def resolve(self, target, *, as_of):
            return Outcome(target, 0.02, END, provenance={"resolver": "custom-1"})

    def metric(view, outcome):
        return {
            **default_metrics(view, outcome),
            "custom_loss": abs(view.statement.value - outcome.value) * 2,
        }

    report = evaluate(archive, Custom(), as_of=END, metric_fn=metric)
    assert report.rows[0].metrics["custom_loss"] == pytest.approx(0.02)
    assert report.rows[0].outcome.provenance["resolver"] == "custom-1"

    class Future:
        def resolve(self, target, *, as_of):
            return Outcome(target, 0.02, END + timedelta(days=1))

    with pytest.raises(ContractError, match="leaked"):
        evaluate(archive, Future(), as_of=END)

    class Wrong:
        def resolve(self, target, *, as_of):
            return Outcome(replace(target, subject=B), 0.02, END)

    with pytest.raises(ContractError, match="changed"):
        evaluate(archive, Wrong(), as_of=END)
    with pytest.raises(ContractError, match="None"):
        evaluate(archive, Custom(), as_of=END, metric_fn=lambda v, o: {"bad": np.nan})


def test_receipt_after_target_end_is_flagged_as_retrospective_evidence():
    view = ReturnView.create(A, INTERVAL, 0.1, 0.01, metadata("late"))
    archive = archive_views(view, received_at=END)
    report = evaluate(archive, ReturnOutcomeResolver(anchors), as_of=END)
    assert report.coverage["issued_after_target_end"] == 1
    assert report.summary()[0]["issued_after_target_end"] == 1


def test_distribution_dimensions_and_missing_density_are_explicit():
    target = return_target(A, INTERVAL)
    joint_law = GaussianReturns((0, 0), ((0.04, 0), (0, 0.04)))
    mismatch = DistributionView(target, Distribution(joint_law), metadata("mismatch"))
    with pytest.raises(ContractError, match="dimension"):
        default_metrics(mismatch, Outcome(target, 0.1, END))

    class UnsupportedLaw:
        units = "decimal"
        support = (-1, float("inf"))

    unsupported = DistributionView(target, Distribution(UnsupportedLaw()), metadata("unsupported"))
    report = evaluate(archive_views(unsupported), ReturnOutcomeResolver(anchors), as_of=END)
    assert report.rows[0].status == "unsupported"
    assert "density" in report.rows[0].reason
    assert report.coverage["scored"] == 0
