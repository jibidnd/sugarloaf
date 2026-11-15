from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import log

import numpy as np
import pytest
from scipy.stats import multivariate_normal

from sugarloaf.core.data import Observation
from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Distribution,
    DistributionView,
    PositionView,
    Probability,
    ProbabilityView,
    QuantileDistribution,
    RankingView,
    RelativeReturnView,
    ReturnEvent,
    ReturnView,
    Score,
    ScoreView,
    Target,
    View,
    min_relative_return,
    min_return,
    return_target,
)
from sugarloaf.research import (
    ForecastArchive,
    Outcome,
    ReturnOutcomeResolver,
    default_metrics,
    evaluate,
)


@dataclass(frozen=True)
class GaussianReturns(Distribution):
    """Test-only joint density; scoring depends on the law protocol, not a model."""

    location: tuple
    covariance: tuple
    units: str = "decimal"
    support: tuple = (-float("inf"), float("inf"))

    @property
    def dimension(self):
        return len(self.location)

    def logpdf(self, value):
        return float(multivariate_normal.logpdf(value, self.location, self.covariance))


@dataclass(frozen=True)
class EventMasses(Distribution):
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


def archive_views(*views, received_at=START):
    archive = ForecastArchive()
    archive.record("signal", "forecasts", views, received_at)
    return archive


def anchors(instrument, at, convention, as_of):
    assert convention in {"price_return", "total_return", "abs_log_close_return"}
    value = 100 if at == START else {A: 110, B: 105, C: 90}[instrument]
    return Observation(at, value)


def test_archive_preserves_replaced_expired_forecasts_and_first_emission_provenance():
    first = ReturnView.create(
        A, INTERVAL, 0.1, 0.03, metadata("first", expires=START + timedelta(days=1))
    )
    receipt = START + timedelta(seconds=2)
    archive = archive_views(first, received_at=receipt)
    archive.record("signal", "forecasts", (first,), START + timedelta(hours=1))
    revised = ReturnView.create(
        A, INTERVAL, 0.05, 0.03, metadata("revised", START + timedelta(days=1))
    )
    archive.record("signal", "forecasts", (revised,), START + timedelta(days=1))
    archive.record("signal", "forecasts", (), END + timedelta(days=2))
    assert len(archive) == 2
    retained = archive.get("first")
    assert retained.view is first
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
        archive.record("signal", "forecasts", (new, bad), START)
    assert len(archive) == 1
    action = PositionView({A: 0.5}, "weight", "branch", metadata("position"))
    archive.record("signal", "positions", (action,), START)
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
    move = Target(A, "move_size", INTERVAL, convention="abs_log_close_return")
    requests.clear()
    outcome = resolver.resolve(move, as_of=END)
    assert outcome.value == pytest.approx(abs(log(1.1)))
    assert all(call[2] == "abs_log_close_return" for call in requests)
    other = replace(move, convention="price_return")
    assert resolver.resolve(other, as_of=END).status == "unsupported"


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
        lambda i, at, c, cutoff: Observation(at, 100, END + timedelta(days=1))
    )
    assert delayed.resolve(target, as_of=END).status == "unavailable"
    substituted = ReturnOutcomeResolver(
        lambda i, at, c, cutoff: Observation(at + timedelta(minutes=1), 100)
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
    first = DistributionView(target, independent, metadata("independent"))
    second = DistributionView(target, correlated, metadata("correlated"))
    assert default_metrics(first, outcome)["log_score"] == pytest.approx(
        -independent.logpdf(outcome.value)
    )
    assert (
        default_metrics(first, outcome)["log_score"]
        != default_metrics(second, outcome)["log_score"]
    )
    scalar = return_target(A, INTERVAL)
    law = EventMasses((ReturnEvent(upper=0), ReturnEvent(lower=0)), (1, 0))
    bins = DistributionView(scalar, law, metadata("discrete"))
    assert default_metrics(bins, Outcome(scalar, 0.1, END))["log_loss"] == float("inf")


def test_raw_scores_get_chronological_cohort_association_not_an_invented_return_error():
    archive = ForecastArchive()
    for k in range(4):
        issued = START + timedelta(days=k)
        target = return_target(A, Interval(issued, issued + timedelta(days=10)))
        view = ScoreView(target, Score(float(k), "signal-v1"), metadata(str(k), issued))
        archive.record("signal", "scores", (view,), issued)

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
    # A joint distribution cannot describe a scalar target; the View rejects it.
    with pytest.raises(ContractError, match="dimension"):
        DistributionView(target, joint_law, metadata("mismatch"))

    class NoCdf(Distribution):
        units = "decimal"
        support = (-1, float("inf"))

    # A scalar law states a cdf; one without it is refused when the View is built.
    with pytest.raises(ContractError, match="must implement cdf"):
        DistributionView(target, NoCdf(), metadata("no-cdf"))

    class UnsupportedLaw(Distribution):
        """A joint law with no density."""

        units = "decimal"
        dimension = 2

    pair = Target((A, B), "return", INTERVAL)
    unsupported = DistributionView(pair, UnsupportedLaw(), metadata("unsupported"))
    report = evaluate(archive_views(unsupported), ReturnOutcomeResolver(anchors), as_of=END)
    assert report.rows[0].status == "unsupported"
    assert "density" in report.rows[0].reason
    assert report.coverage["scored"] == 0


# Window minima: daily closes of A and B over INTERVAL, both 100 at START. Against B, A's
# path is d1 +0.03, d2 -0.03, d3 -0.08 - (-0.02) = -0.06, d4 -0.03, d5 -0.02, d6 -0.04,
# d7 +0.03, d8 +0.04, d9 +0.05, d10 +0.05: its minimum is -0.06 on day 3. A alone falls
# to -0.08 on day 3.
WINDOW_CLOSES = {
    A: [104, 97, 92, 99, 101, 95, 103, 108, 110, 110],
    B: [101, 100, 98, 102, 103, 99, 100, 104, 105, 105],
}


def window_sources(closes=WINDOW_CLOSES, ends=None):
    """anchor and series over daily closes: day k's close at START + k days."""
    table = {i: {START + timedelta(days=k + 1): float(c) for k, c in enumerate(values)}
             for i, values in closes.items()}
    for instrument in table:
        table[instrument][START] = 100.0

    def anchor(instrument, at, convention, as_of):
        value = table[instrument].get(at)
        return None if value is None else Observation(at, value)

    def series(instrument, start, end, convention, as_of):
        return sorted((t, v) for t, v in table[instrument].items() if start < t <= end)

    return anchor, series


def test_window_minimum_of_a_return_and_of_a_relative_return():
    anchor, series = window_sources()
    resolver = ReturnOutcomeResolver(anchor, series=series)
    alone = resolver.resolve(min_return(A, INTERVAL, "total_return"), as_of=END)
    assert alone.value == pytest.approx(-0.08) and alone.available_at == END
    relative = resolver.resolve(min_relative_return(A, B, INTERVAL, "total_return"), as_of=END)
    assert relative.value == pytest.approx(-0.06)
    assert relative.provenance["unmatched_bars"] == "0"
    # The window ends on day 2: the minimum reads days 1 and 2 only.
    short = Interval(START, START + timedelta(days=2))
    assert resolver.resolve(min_relative_return(A, B, short), as_of=END).value == pytest.approx(
        -0.03)
    # From the close of day 3 (A 92, B 98) to day 6: d4 99/92 - 102/98, d5, d6 95/92 - 99/98.
    later = Interval(START + timedelta(days=3), START + timedelta(days=6))
    expected = min(a / 92 - b / 98 for a, b in ((99, 102), (101, 103), (95, 99)))
    assert resolver.resolve(min_relative_return(A, B, later), as_of=END).value == pytest.approx(
        expected)
    assert resolver.resolve(min_return(A, INTERVAL), as_of=START).status == "pending"


def test_window_minimum_skips_and_counts_unmatched_bars_and_needs_half_of_them():
    anchor, series = window_sources()

    def without(days):
        def thinned(instrument, start, end, convention, as_of):
            rows = series(instrument, start, end, convention, as_of)
            if instrument != B:
                return rows
            return [(t, v) for t, v in rows if (t - START).days not in days]
        return ReturnOutcomeResolver(anchor, series=thinned)

    target = min_relative_return(A, B, INTERVAL)
    # Without B's bar on day 3 that day is skipped: the minimum is day 6's -0.04.
    skipped = without({3}).resolve(target, as_of=END)
    assert skipped.value == pytest.approx(-0.04)
    assert skipped.provenance["unmatched_bars"] == "1"
    # Five of A's ten bars matched is half; four is fewer.
    assert without({1, 2, 3, 4, 5}).resolve(target, as_of=END).status == "resolved"
    fewer = without({1, 2, 3, 4, 5, 6}).resolve(target, as_of=END)
    assert fewer.status == "unsupported" and "fewer than half" in fewer.reason


def test_window_minimum_states_why_it_has_no_label():
    anchor, series = window_sources()
    target = min_relative_return(A, B, INTERVAL)
    bare = ReturnOutcomeResolver(anchor).resolve(target, as_of=END)
    assert bare.status == "unsupported" and "series" in bare.reason
    resolver = ReturnOutcomeResolver(anchor, series=series)
    for broken in (replace(target, convention="abs_log_close_return"),
                   replace(target, time=(INTERVAL, INTERVAL)),
                   replace(target, units="percent"),
                   replace(target, subject=(A, B, C))):
        assert resolver.resolve(broken, as_of=END).status == "unsupported"
    # The last close must be held, as for a return: a path that stops early has no label.
    short = {A: WINDOW_CLOSES[A][:9], B: WINDOW_CLOSES[B]}
    short_anchor, short_series = window_sources(short)
    stopped = ReturnOutcomeResolver(short_anchor, series=short_series).resolve(target, as_of=END)
    assert stopped.status == "unavailable"

    def outside(instrument, start, end, convention, as_of):
        return [*series(instrument, start, end, convention, as_of), (END + timedelta(days=1), 1.0)]

    with pytest.raises(ContractError, match="outside its window"):
        ReturnOutcomeResolver(anchor, series=outside).resolve(target, as_of=END)


def test_a_window_minimum_view_is_one_coordinate_and_scored_as_a_distribution():
    target = min_relative_return(A, B, INTERVAL)
    with pytest.raises(ContractError):
        min_relative_return(A, A, INTERVAL)
    law = QuantileDistribution((0.1, 0.5, 0.9), (-0.10, -0.05, 0.0))
    view = View(target, law, metadata("minimum"))
    with pytest.raises(ContractError, match="dimension"):
        View(target, GaussianReturns((0.0, 0.0), ((1, 0), (0, 1))), metadata("joint"))
    with pytest.raises(ContractError):
        View(target, Probability((ReturnEvent(None, 0.0),), (0.5,)), metadata("event"))
    anchor, series = window_sources()
    report = evaluate(archive_views(view), ReturnOutcomeResolver(anchor, series=series),
                      as_of=END)
    (row,) = report.rows
    assert row.status == "scored" and row.outcome.value == pytest.approx(-0.06)
    # Linear between the 10% and 50% quantiles: cdf(-0.06) = 0.1 + 0.4 * 0.04 / 0.05.
    assert row.metrics["pit"] == pytest.approx(0.42)
    assert row.metrics["log_score"] == pytest.approx(-log(law.pdf(-0.06)))
