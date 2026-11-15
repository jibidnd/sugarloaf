"""View metrics of a run: the pooled diagnostics in ``evaluation`` and ``view_metrics``.

Hand-computed cases: pinball loss, interval coverage, the PIT histogram, sign
classification and rank groups; then a small archive scored against a price table, where
every issued view is counted and a horizon after ``as_of`` stays unscored although the
table holds later prices.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Distribution,
    DistributionView,
    QuantileDistribution,
    Score,
    ScoreView,
    View,
    min_relative_return,
    relative_return,
    return_target,
)
from sugarloaf.research import ForecastArchive
from sugarloaf.research.evaluation import (
    direction_metrics,
    grouped_outcomes,
    pinball_loss,
    pit_histogram,
    quantile_groups,
    quantile_metrics,
)
from sugarloaf.research.view_metrics import (
    PriceTable,
    compact,
    headline,
    reduced,
    target_family,
    view_metrics,
)

SPY = InstrumentId("USA:SPY")
T0 = datetime(2025, 1, 6, 15, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Uniform(Distribution):
    """Uniform returns on [low, high]: quantile(p) = low + p (high - low)."""

    low: float = -0.01
    high: float = 0.01
    units: str = "decimal"

    @property
    def support(self):
        return (self.low, self.high)

    def cdf(self, value):
        return min(1.0, max(0.0, (value - self.low) / (self.high - self.low)))


def test_pinball_loss_weights_each_side_by_its_level():
    assert pinball_loss(0.02, 0.0, 0.9) == pytest.approx(0.002)
    assert pinball_loss(0.02, 0.03, 0.9) == pytest.approx(0.009)
    assert pinball_loss(0.0, 0.0, 0.5) == 0


def test_quantile_metrics_of_a_uniform_law():
    metrics = quantile_metrics(Uniform(), 0.004)
    # q(0.9) = 0.008, above the value: (0.008 - 0.004) * 0.1.
    assert metrics["pinball_0.9"] == pytest.approx(0.0004)
    assert metrics["median"] == pytest.approx(0.0, abs=1e-12)
    # Central 50%: [-0.005, 0.005] holds 0.004; central 90%: [-0.009, 0.009] holds it.
    assert (metrics["covered_0.5"], metrics["covered_0.9"]) == (1.0, 1.0)
    assert quantile_metrics(Uniform(), 0.006)["covered_0.5"] == 0.0


def test_pit_histogram_puts_one_in_the_last_bin():
    assert pit_histogram([0.0, 0.05, 0.55, 1.0]) == [2, 0, 0, 0, 0, 1, 0, 0, 0, 1]


def test_direction_metrics_with_up_as_positive():
    scores = [1, 1, -1, -1, 0]
    realized = [0.01, -0.02, -0.01, 0.03, 0.01]
    metrics = direction_metrics(scores, realized)
    # Nonzero pairs: 4, of which (1, +) and (-1, -) agree.
    assert (metrics["hit_rate"], metrics["hit_count"]) == (0.5, 4)
    # Predicted up: 2 rows, 1 up. Up: 3 rows, 1 predicted up.
    assert metrics["precision"] == 0.5 and metrics["recall"] == pytest.approx(1 / 3)
    assert metrics["f1"] == pytest.approx(0.4)


def test_tied_keys_share_a_group():
    assert quantile_groups(range(10), 5) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert len(set(quantile_groups([0.0] * 10, 10))) == 1
    (group,) = grouped_outcomes([1.0] * 4, [0.1, 0.2, 0.3, 0.4], 10, quantiles=(0.5,))
    assert (group["count"], group["mean"], group["q0.5"]) == (4, pytest.approx(0.25),
                                                              pytest.approx(0.25))


def _closes(minutes, start=100.0, step=0.1):
    return {SPY: [(T0 + k * MINUTE, start + step * k) for k in range(minutes + 1)]}


def _score(archive, k, value, horizon_minutes, node="signal"):
    issued = T0 + k * MINUTE
    end = issued + horizon_minutes * MINUTE
    view = ScoreView(return_target(SPY, Interval(issued, end)), Score(value, "test:v1"),
                     Metadata(f"{node}:{k}", node, issued, end))
    archive.record(node, "views", (view,), issued)


def test_every_issued_view_is_counted_and_no_later_price_scores_an_open_horizon():
    archive = ForecastArchive()
    for k in range(10):
        _score(archive, k, 1.0 if k % 2 else -1.0, 10)
    _score(archive, 3, 0.5, 100, node="slow")      # ends after as_of
    _score(archive, 4, 0.5, 21, node="slow")       # ends at minute 25, where no bar exists
    prices = PriceTable({"price_return": {SPY: [
        (end, close) for end, close in _closes(60)[SPY] if end != T0 + 25 * MINUTE]}})
    as_of = T0 + 30 * MINUTE  # prices run to minute 60; the 100-minute horizon is open
    metrics = view_metrics(archive, prices, as_of)
    counts = metrics["counts"]
    assert counts["issued"] == 12
    assert counts["scored"] + counts["horizon_after_end"] + counts["no_bar"] \
        + counts["unsupported"] == 12
    assert (counts["scored"], counts["horizon_after_end"], counts["no_bar"]) == (10, 1, 1)
    assert (counts["no_bar_at_start"], counts["no_bar_at_end"]) == (0, 1)
    score = metrics["kinds"]["score"]
    # Prices only rise: positive scores hit, negative ones miss.
    assert score["pooled"]["hit_rate"] == 0.5 and score["pooled"]["precision"] == 1.0
    assert set(score["by_node"]) == {"signal"}
    # The path of a view from minute k over ten minutes: close(k + j) / close(k) - 1.
    first = score["pooled"]["deciles"][0]
    assert len(first["path"]) == 10 and first["path"][-1] == first["mean"]


def test_a_view_from_a_bar_start_is_scored_from_that_bars_open():
    # Daily bars from 09:30 to 16:00; a view issued at the open ends at the close.
    day = 24 * 60 * MINUTE
    bars = [(T0 + k * day, T0 + k * day + 390 * MINUTE, 100.0 + k, 101.0 + k) for k in range(5)]
    closes = {SPY: [(end, close) for _, end, _, close in bars]}
    opens = {SPY: [(start, open_) for start, _, open_, _ in bars]}
    archive = ForecastArchive()
    start, end = bars[1][0], bars[1][1]
    view = ScoreView(return_target(SPY, Interval(start, end)), Score(1.0, "test:v1"),
                     Metadata("open:1", "signal", start, end))
    archive.record("signal", "views", (view,), start)
    without = view_metrics(archive, PriceTable({"price_return": closes}), bars[-1][1])
    assert (without["counts"]["no_bar"], without["counts"]["no_bar_at_start"]) == (1, 1)
    table = PriceTable({"price_return": closes}, {"price_return": opens})
    assert table.has("price_return", SPY, start) and table.has("price_return", SPY, end)
    scored = view_metrics(archive, table, bars[-1][1])
    assert scored["counts"]["scored"] == 1
    # Open 101 to close 102: the realized return is 102 / 101 - 1.
    decile = [d for d in scored["kinds"]["score"]["pooled"]["deciles"] if d["count"]][0]
    assert decile["mean"] == pytest.approx(102 / 101 - 1)


def test_distribution_views_get_pinball_pit_coverage_and_quintiles():
    archive = ForecastArchive()
    for k in range(10):
        issued = T0 + k * MINUTE
        end = issued + 10 * MINUTE
        view = DistributionView(return_target(SPY, Interval(issued, end)),
                                Uniform(-0.02, 0.02), Metadata(f"d:{k}", "dist", issued, end))
        archive.record("dist", "views", (view,), issued)
    prices = PriceTable({"price_return": _closes(30, step=0.0)})  # flat: every return is 0
    summary = view_metrics(archive, prices, T0 + 30 * MINUTE)["kinds"]["distribution"]["pooled"]
    assert summary["count"] == 10
    assert summary["pit_histogram"] == [0] * 5 + [10] + [0] * 4  # cdf(0) = 0.5
    assert (summary["coverage_50"], summary["coverage_90"]) == (1.0, 1.0)
    assert summary["pinball"]["0.5"] == pytest.approx(0.0, abs=1e-9)
    assert summary["log_score_mean"] == pytest.approx(-3.21888, rel=1e-5)  # -log(1 / 0.04)
    (quintile,) = summary["quintiles"]  # one median, so one group
    assert quintile["count"] == 10 and quintile["path"] == [0.0] * 10


def test_compact_rounds_and_keeps_json_standard():
    assert compact({"a": 1 / 3, "b": float("inf"), "c": float("nan"), "d": [2.0]}) == {
        "a": 0.333333, "b": "inf", "c": None, "d": [2.0]}


def test_reduced_detail_and_headline():
    archive = ForecastArchive()
    for k in range(10):
        _score(archive, k, float(k - 5), 10)
    metrics = view_metrics(archive, PriceTable({"price_return": _closes(30)}), T0 + 30 * MINUTE)
    assert "deciles" not in reduced(metrics, 1)["kinds"]["score"]["by_node"]["signal"]
    assert "by_node" not in reduced(metrics, 2)["kinds"]["score"]
    stats = headline(metrics)
    assert stats["Views issued"] == "10" and stats["Views scored"] == "10"
    assert {"Score hit rate", "Score F1", "Score decile spread"} <= set(stats)


IWC = InstrumentId("USA:IWC")
DAY = 24 * 60 * MINUTE


def _daily(opens_closes):
    """Daily bars 09:30-16:00 from (open, close) pairs: (start, end, open, close)."""
    return [(T0 + k * DAY, T0 + k * DAY + 390 * MINUTE, o, c)
            for k, (o, c) in enumerate(opens_closes)]


def test_price_table_series_holds_the_closes_inside_the_window_up_to_as_of():
    bars = _daily([(100, 101), (101, 99), (99, 104), (104, 103)])
    table = PriceTable({"total_return": {SPY: [(e, c) for _, e, _, c in bars]}},
                       {"total_return": {SPY: [(s, o) for s, _, o, _ in bars]}})
    # From day 1's open: the closes of days 1 to 3, not day 0's close nor the opens.
    assert table.series(SPY, bars[1][0], bars[3][1], "total_return", bars[3][1]) == [
        (bars[1][1], 99), (bars[2][1], 104), (bars[3][1], 103)]
    # From day 1's close the window starts after it; as_of cuts the rest.
    assert table.series(SPY, bars[1][1], bars[3][1], "total_return", bars[2][1]) == [
        (bars[2][1], 104)]
    assert table.series(SPY, bars[1][1], bars[3][1], "price_return", bars[3][1]) == []


def test_window_minimum_views_are_scored_as_distributions_in_their_own_family():
    # Stock opens 50 on day 1 and closes 47, 44, 48 on days 1-3; IWC opens 100, closes
    # 99, 101, 102. Against IWC from day 1's open: -0.05, -0.13, -0.06, so the minimum is
    # -0.13; the return at day 3's close is 48/50 - 102/100 = -0.06.
    stock = _daily([(49, 50), (50, 47), (47, 44), (44, 48), (48, 49)])
    bench = _daily([(100, 100), (100, 99), (99, 101), (101, 102), (102, 102)])
    table = PriceTable(
        {"total_return": {SPY: [(e, c) for _, e, _, c in stock],
                          IWC: [(e, c) for _, e, _, c in bench]}},
        {"total_return": {SPY: [(s, o) for s, _, o, _ in stock],
                          IWC: [(s, o) for s, _, o, _ in bench]}},
    )
    window = Interval(stock[1][0], stock[3][1])
    law = QuantileDistribution((0.1, 0.5, 0.9), (-0.20, -0.10, 0.00))
    archive = ForecastArchive()
    meta = Metadata("s1:min", "s1", window.start, window.end)
    archive.record("s1", "views", (View(min_relative_return(SPY, IWC, window, "total_return"),
                                        law, meta),), window.start)
    at = View(relative_return(SPY, IWC, window, "total_return"), law,
              Metadata("s1:at", "s1", window.start, window.end))
    archive.record("s1", "views", (at,), window.start)
    metrics = view_metrics(archive, table, stock[-1][1])
    assert metrics["counts"]["scored"] == 2 and metrics["counts"]["unmatched_bars"] == 0
    families = metrics["kinds"]["distribution"]["by_family"]
    assert set(families) == {"difference:USA:IWC", "min_relative_return:USA:IWC"}
    minimum = families["min_relative_return:USA:IWC"]
    (group,) = minimum["quintiles"]
    assert group["mean"] == pytest.approx(-0.13)
    # Linear between the 10% and 50% quantiles: cdf(-0.13) = 0.1 + 0.4 * 0.07 / 0.1 = 0.38.
    assert minimum["pit_histogram"][3] == 1
    assert group["path"] is None  # the path rows read single-instrument and difference targets
    assert families["difference:USA:IWC"]["quintiles"][0]["mean"] == pytest.approx(-0.06)
    assert metrics["by_family_counts"]["min_relative_return:USA:IWC"]["scored"] == 1
    assert "by_family" in reduced(metrics, 1)["kinds"]["distribution"]
    assert "quintiles" not in reduced(metrics, 1)["kinds"]["distribution"]["by_family"][
        "min_relative_return:USA:IWC"]
    assert "by_family_counts" not in reduced(metrics, 2)
    # One family: no per-family breakdown.
    single = ForecastArchive()
    single.record("s1", "views", (at,), window.start)
    assert "by_family_counts" not in view_metrics(single, table, stock[-1][1])
    assert target_family(return_target(SPY, window)) == "return"
