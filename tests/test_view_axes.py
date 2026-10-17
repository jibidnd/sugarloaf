"""Views as target x representation, and the quantile distribution's numerics."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from scipy.integrate import quad

from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.node import InputSpec, NodeInputs
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Distribution,
    Estimate,
    Ordering,
    Probability,
    QuantileDistribution,
    ReturnEvent,
    ReturnView,
    Score,
    Target,
    View,
    relative_return,
    return_target,
)
from sugarloaf.nodes.simple import MeanViewAggregator, PositiveSignalPositions
from sugarloaf.research.evaluation import Outcome, default_metrics

STOCK, BENCH = InstrumentId("STOCK"), InstrumentId("BENCH")
NOW = datetime(2025, 1, 2, tzinfo=UTC)
END = NOW + timedelta(days=30)
INTERVAL = Interval(NOW, END)
META = Metadata("view", "node", NOW, END + timedelta(days=1))
CTX = SimpleNamespace(node_id="agg", snapshot_id=1, now=NOW)

LEVELS = (0.1, 0.25, 0.5, 0.75, 0.9)
VALUES = (-0.55, -0.3, -0.12, 0.0, 0.2)


def quantile_law(**kwargs):
    return QuantileDistribution(LEVELS, VALUES, **kwargs)


@pytest.mark.parametrize("lower_bound", [None, -1.0])
def test_quantile_distribution_moments_match_numerical_integration(lower_bound):
    law = quantile_law(lower_bound=lower_bound)
    low = -50.0 if lower_bound is None else lower_bound
    points = list(VALUES)
    # Integrate the density piece by piece; its kinks sit at the stated quantiles.
    mass = quad(law.pdf, low, 50.0, points=points, limit=200)[0]
    mean = quad(lambda x: x * law.pdf(x), low, 50.0, points=points, limit=200)[0]
    second = quad(lambda x: x * x * law.pdf(x), low, 50.0, points=points, limit=200)[0]
    assert mass == pytest.approx(1.0, abs=1e-8)
    assert law.mean() == pytest.approx(mean, abs=1e-8)
    assert law.variance() == pytest.approx(second - mean**2, abs=1e-8)


def test_quantile_distribution_reproduces_its_quantiles_and_inverts():
    law = quantile_law(lower_bound=-1.0)
    for level, value in zip(LEVELS, VALUES):
        assert law.quantile(level) == pytest.approx(value)
        assert law.cdf(value) == pytest.approx(level)
    for p in (0.001, 0.05, 0.3, 0.6, 0.95, 0.9999):
        assert law.cdf(law.quantile(p)) == pytest.approx(p, abs=1e-12)
    assert law.quantile(0) == -1.0 and law.cdf(-1.0) == 0.0
    assert law.probability(-0.3, 0.0) == pytest.approx(0.5)
    assert law.probability(upper=-0.12) == pytest.approx(0.5)


@pytest.mark.parametrize("lower_bound", [None, -1.0, -0.8])
def test_default_tail_scales_make_the_density_continuous_at_the_outer_quantiles(lower_bound):
    law = quantile_law(lower_bound=lower_bound)
    eps = 1e-9
    assert law.pdf(VALUES[0] - eps) == pytest.approx(law.pdf(VALUES[0] + eps), rel=1e-6)
    assert law.pdf(VALUES[-1] - eps) == pytest.approx(law.pdf(VALUES[-1] + eps), rel=1e-6)


def test_a_fatter_upper_tail_raises_the_mean_by_its_mass_times_the_extra_scale():
    base = quantile_law()
    fat = quantile_law(tail_scale_high=base.tail_scale_high + 0.4)
    assert fat.mean() - base.mean() == pytest.approx((1 - LEVELS[-1]) * 0.4)


@pytest.mark.parametrize(
    "levels, values, extra, message",
    [
        (LEVELS, (-0.5, -0.3, -0.3, 0.0, 0.2), {}, "strictly increasing"),
        ((0.0, 0.5, 1.0), (-0.1, 0.0, 0.1), {}, "inside"),
        (LEVELS, VALUES, {"lower_bound": -0.5}, "lower bound"),
        (LEVELS, VALUES, {"tail_scale_high": -0.1}, "positive"),
        ((0.5,), (0.0,), {}, "at least two"),
    ],
)
def test_quantile_distribution_rejects_malformed_input(levels, values, extra, message):
    with pytest.raises(ContractError, match=message):
        QuantileDistribution(levels, values, **extra)


def test_one_view_type_carries_any_valid_target_and_representation():
    relative = relative_return(STOCK, BENCH, INTERVAL)
    view = View(relative, quantile_law(), META)
    assert view.statement.mean() == pytest.approx(quantile_law().mean())
    View(relative, Estimate(-0.1, 0.2), META)
    View(return_target(STOCK, INTERVAL), Probability((ReturnEvent(lower=0),), (0.4,)), META)
    View(Target((STOCK, BENCH), "return", INTERVAL), Ordering(((STOCK,), (BENCH,)), 1.0), META)
    View(Target("market", "regime", INTERVAL), Score(1.0, "regime-flag"), META)


def test_invalid_target_and_representation_pairs_are_rejected():
    joint = Target((STOCK, BENCH), "return", INTERVAL)
    with pytest.raises(ContractError, match="scalar or relative"):
        View(joint, Estimate(0.01, 0.02), META)
    with pytest.raises(ContractError, match="dimension"):
        View(joint, quantile_law(), META)
    with pytest.raises(ContractError, match="units"):
        View(Target(STOCK, "return", INTERVAL, units="USD"), quantile_law(), META)
    with pytest.raises(ContractError, match="Ranking target"):
        View(joint, Ordering(((BENCH,), (STOCK,)), 1.0), META)
    with pytest.raises(ContractError, match="Representation"):
        View(joint, 0.1, META)


def test_representations_answer_only_what_they_state():
    estimate = Estimate(0.02, 0.05)
    assert estimate.supports("mean") and estimate.mean() == 0.02
    with pytest.raises(CapabilityError):
        estimate.quantile(0.5)
    median = Estimate(0.01, 0.05, property="quantile", level=0.5)
    assert median.quantile(0.5) == 0.01
    with pytest.raises(CapabilityError):
        median.quantile(0.9)
    probability = Probability((ReturnEvent(lower=0),), (0.6,))
    assert probability.probability(lower=0) == 0.6
    with pytest.raises(CapabilityError):
        probability.probability(upper=0)
    with pytest.raises(CapabilityError):
        probability.mean()
    assert not Score(1.0, "x").supports("mean")
    stated = {"mean", "variance", "quantile", "cdf", "pdf", "probability"}
    assert stated <= quantile_law().capabilities
    with pytest.raises(ContractError, match="increasing"):
        quantile_law().probability(0.1, -0.1)


class QuantileOnly(Distribution):
    """A Cauchy law: it has a quantile function but no mean."""

    def quantile(self, p):
        return math.tan(math.pi * (p - 0.5))


class BrokenDensity(Distribution):
    def pdf(self, value):
        return float("nan")


def test_a_distribution_states_its_moments_and_does_not_infer_them():
    law = QuantileOnly()
    assert law.supports("quantile") and not law.supports("mean")
    with pytest.raises(CapabilityError):
        law.mean()


def test_an_invalid_density_is_an_error_not_a_zero():
    with pytest.raises(ContractError, match="invalid density"):
        BrokenDensity().logpdf(0.0)


def test_a_distribution_field_cannot_hide_a_query():
    with pytest.raises(TypeError, match="reserved"):

        @dataclass(frozen=True)
        class Shadowing(Distribution):
            mean: float


def test_input_ports_can_require_a_mean_from_any_representation():
    spec = InputSpec((View,), needs=("mean",))
    target = return_target(STOCK, INTERVAL)
    mean_estimate = ReturnView(target, Estimate(0.01, 0.02), META)
    spec.validate((mean_estimate, View(target, quantile_law(), META)))
    with pytest.raises(ContractError, match="mean"):
        spec.validate((View(target, Score(1.0, "x"), META),))


def test_aggregator_averages_mean_estimates_and_refuses_distributions():
    target = return_target(STOCK, INTERVAL)
    named = ReturnView(target, Estimate(0.02, 0.05), Metadata("a", "a", NOW, END))
    plain = View(target, Estimate(0.04, 0.03), Metadata("b", "b", NOW, END))
    (output,) = MeanViewAggregator().evaluate(
        NodeInputs({"views": (named, plain)}), CTX
    ).by_port["views"]
    assert output.statement.mean() == pytest.approx(0.03)
    assert output.statement.forecast_error_scale == 0.05
    assert output.metadata.parents == ("a", "b")
    # A distribution's spread is outcome risk, not the uncertainty of its mean.
    distribution = View(target, quantile_law(lower_bound=-1.0), Metadata("c", "c", NOW, END))
    with pytest.raises(ContractError, match="not distributions"):
        MeanViewAggregator().evaluate(NodeInputs({"views": (distribution,)}), CTX)


def test_positions_read_the_mean_of_a_distribution():
    target = return_target(STOCK, INTERVAL)
    law = quantile_law(lower_bound=-1.0)
    assert law.mean() < 0
    view = View(target, law, Metadata("d", "d", NOW, END))
    (positions,) = PositiveSignalPositions((STOCK,), budget=1.0).evaluate(
        NodeInputs({"views": (view,)}), CTX
    ).by_port["positions"]
    assert positions.positions[STOCK] == 0


def test_a_quantile_distribution_is_scored_by_log_density_and_pit():
    target = relative_return(STOCK, BENCH, INTERVAL)
    law = quantile_law()
    metrics = default_metrics(View(target, law, META), Outcome(target, -0.12, END))
    assert metrics["log_score"] == pytest.approx(-math.log(law.pdf(-0.12)))
    assert metrics["pit"] == pytest.approx(0.5)
