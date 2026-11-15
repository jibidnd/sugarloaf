"""The Distribution contract: a scalar law states cdf and the base class derives the rest."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from scipy import stats

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Distribution,
    QuantileDistribution,
    ScaledHalfT,
    Target,
    View,
)
from sugarloaf.research.archive import ForecastArchive
from sugarloaf.research.serialization import decode_archive, encode_archive

NOW = datetime(2025, 1, 2, tzinfo=UTC)
INTERVAL = Interval(NOW, NOW + timedelta(days=1))
LEVELS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


class CdfOnly(Distribution):
    """A law that states only its cdf: here the cdf of a ScaledHalfT."""

    support = (0.0, math.inf)

    def __init__(self, law):
        self.law = law

    def cdf(self, value):
        return self.law.cdf(value)


class LogisticCdf(Distribution):
    """A logistic law (location 0.01, scale 0.02) stated by its cdf alone."""

    def cdf(self, value):
        return 0.5 * (1 + math.tanh((value - 0.01) / 0.04))


def move_size_target():
    return Target(InstrumentId("STOCK"), "abs_return", INTERVAL, units="decimal")


@pytest.mark.parametrize("df", [2.3, 3.0, 6.0, 40.0])
def test_derived_methods_match_the_half_t_closed_forms(df):
    law = ScaledHalfT(0.012, df, calibration=1.1)
    derived = CdfOnly(law)
    for p in LEVELS:
        assert derived.quantile(p) == pytest.approx(law.quantile(p), rel=1e-8)
    for y in (0.0, 0.001, 0.01, 0.05, 0.3):
        # Central differences of a cdf near 1 are accurate in absolute, not relative, terms.
        assert derived.pdf(y) == pytest.approx(law.pdf(y), rel=1e-6, abs=1e-8)
    assert derived.mean() == pytest.approx(law.mean(), rel=1e-8)
    assert derived.variance() == pytest.approx(law.variance(), rel=1e-5)


def test_half_t_closed_forms_match_scipy_student_t():
    law = ScaledHalfT(0.02, 3.5, calibration=0.8)
    width = 0.016
    t = stats.t(3.5)
    for y in (0.001, 0.02, 0.2):
        assert law.cdf(y) == pytest.approx(2 * t.cdf(y / width) - 1, rel=1e-12)
        assert law.pdf(y) == pytest.approx(2 * t.pdf(y / width) / width, rel=1e-12)
    for p in LEVELS:
        assert law.quantile(p) == pytest.approx(width * t.ppf((1 + p) / 2), rel=1e-10)
        assert law.cdf(law.quantile(p)) == pytest.approx(p, abs=1e-12)
    mean = width * 2 * t.expect(lambda x: x, lb=0)
    assert law.mean() == pytest.approx(mean, rel=1e-8)
    assert law.variance() == pytest.approx(width**2 * 3.5 / 1.5 - mean**2, rel=1e-8)
    assert law.cdf(-0.1) == 0.0 and law.pdf(-0.1) == 0.0 and law.quantile(1) == math.inf


def test_calibration_multiplies_the_scale_inside_the_cdf():
    plain, wide = ScaledHalfT(0.01, 4.0), ScaledHalfT(0.01, 4.0, calibration=2.0)
    assert plain.calibration == 1.0
    assert wide.cdf(0.02) == pytest.approx(plain.cdf(0.01))
    assert wide.quantile(0.9) == pytest.approx(2 * plain.quantile(0.9))
    assert wide.mean() == pytest.approx(2 * plain.mean())


@pytest.mark.parametrize(
    "scale, df, calibration",
    [(0.0, 3.0, 1.0), (-0.01, 3.0, 1.0), (0.01, 2.0, 1.0), (0.01, 3.0, 0.0),
     (math.nan, 3.0, 1.0), (0.01, math.inf, 1.0), (True, 3.0, 1.0), ("0.01", 3.0, 1.0),
     (1e300, 3.0, 1e300)],
)
def test_half_t_rejects_invalid_parameters(scale, df, calibration):
    with pytest.raises(ContractError):
        ScaledHalfT(scale, df, calibration)


def test_a_law_that_states_only_cdf_gets_every_query():
    law = LogisticCdf()
    law.check()
    assert {"quantile", "pdf", "logpdf", "mean", "variance", "probability"} <= law.capabilities
    logistic = stats.logistic(0.01, 0.02)
    for p in (1e-6, 0.1, 0.5, 0.9, 1 - 1e-6):
        assert law.quantile(p) == pytest.approx(logistic.ppf(p), abs=1e-11)
    assert law.pdf(0.03) == pytest.approx(logistic.pdf(0.03), rel=1e-7)
    assert law.logpdf(0.03) == pytest.approx(logistic.logpdf(0.03), rel=1e-7)
    assert law.mean() == pytest.approx(0.01, abs=1e-9)
    assert law.variance() == pytest.approx(logistic.var(), rel=1e-6)
    assert law.probability(0.0, 0.02) == pytest.approx(logistic.cdf(0.02) - logistic.cdf(0.0))
    assert law.quantile(0) == -math.inf and law.quantile(1) == math.inf
    with pytest.raises(ContractError):
        law.quantile(1.5)


def test_check_rejects_a_cdf_that_decreases_or_does_not_reach_one():
    class Decreasing(Distribution):
        def cdf(self, value):
            return 0.5 + 0.4 * math.sin(value)

    class Defective(Distribution):
        def cdf(self, value):
            return 0.0 if value < 0 else 0.5

    with pytest.raises(ContractError, match="decreases"):
        Decreasing().check()
    with pytest.raises(ContractError, match="tend to 0 and 1"):
        Defective().check()
    with pytest.raises(ContractError, match="does not reach 1"):
        Defective().quantile(0.9)


def test_quantile_distribution_keeps_its_closed_forms():
    law = QuantileDistribution(LEVELS, (0.001, 0.002, 0.005, 0.01, 0.02, 0.035, 0.05),
                               lower_bound=0.0)
    law.check()
    for p, q in zip(LEVELS, law.values):
        assert law.quantile(p) == pytest.approx(q, rel=1e-14)
    # The base class's numerical routes agree with the stated closed forms.
    assert Distribution.mean(law) == pytest.approx(law.mean(), rel=1e-6)
    assert Distribution.quantile(law, 0.3) == pytest.approx(law.quantile(0.3), abs=1e-11)


def test_half_t_views_round_trip_through_the_archive_serializer():
    law = ScaledHalfT(0.0123, 2.9, calibration=1.07)
    view = View(move_size_target(), law, Metadata("v1", "move_size", NOW, NOW + timedelta(days=2)))
    archive = ForecastArchive()
    archive.record("move_size", "views", (view,), NOW)
    encoded = encode_archive(archive)
    statement = encoded[0]["view"]["statement"]
    # Field by field, so a research kernel decodes it without the producing package.
    assert statement == {
        "type": "ScaledHalfT",
        "fields": {"scale": 0.0123, "df": 2.9, "calibration": 1.07, "units": "decimal"},
    }
    (record,) = decode_archive(encoded)
    assert record.view == view
    assert record.view.statement.quantile(0.5) == law.quantile(0.5)
