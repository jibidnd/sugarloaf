"""Block-bootstrap intervals and cross-sectional rank correlation on controlled inputs."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.node import NodeOutputs
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import Score, ScoreView, return_target
from sugarloaf.market.contracts import Observation
from sugarloaf.research import (
    ForecastArchive,
    ReturnOutcomeResolver,
    block_bootstrap_interval,
    cross_sectional_ic,
    evaluate,
)

START = datetime(2025, 1, 6, 21, tzinfo=UTC)  # A Monday close.
INSTRUMENTS = tuple(InstrumentId(name) for name in "ABCD")


def test_block_bootstrap_is_reproducible_and_widens_for_dependent_data():
    generator = np.random.default_rng(0)
    # Each value is repeated ten times in a row: 400 observations, but only 40
    # independent ones. This is what overlapping forecasts look like.
    dependent = np.repeat(generator.normal(size=40), 10)
    naive = block_bootstrap_interval(dependent, block=1, seed=7)
    blocked = block_bootstrap_interval(dependent, block=10, seed=7)
    assert naive["estimate"] == blocked["estimate"] == pytest.approx(dependent.mean())
    # Treating the 400 as independent understates the uncertainty of the mean.
    assert (blocked["high"] - blocked["low"]) > 2 * (naive["high"] - naive["low"])
    assert blocked["low"] < blocked["estimate"] < blocked["high"]
    # The same seed reproduces the same interval; a different seed does not.
    assert block_bootstrap_interval(dependent, block=10, seed=7) == blocked
    assert block_bootstrap_interval(dependent, block=10, seed=8) != blocked


def test_block_bootstrap_resamples_paired_columns_on_the_same_dates():
    generator = np.random.default_rng(1)
    common = generator.normal(size=300)
    # Two series that share almost all of their noise, one 0.1 higher on average.
    paired = np.column_stack([common + 0.1, common + generator.normal(scale=0.01, size=300)])

    def difference(sample):
        return float(np.mean(sample[:, 0] - sample[:, 1]))

    result = block_bootstrap_interval(paired, block=5, seed=3, statistic=difference)
    # Resampling rows together cancels the shared noise, so the small difference
    # is cleanly resolved even though each series alone is far noisier than 0.1.
    assert 0.09 < result["low"] < 0.1 < result["high"] < 0.11


@pytest.mark.parametrize(
    "values, options",
    [
        ([1.0], {"block": 1}),
        ([1.0, float("nan"), 2.0], {"block": 1}),
        ([1.0, 2.0, 3.0], {"block": 4}),
        ([1.0, 2.0, 3.0], {"block": True}),
        ([1.0, 2.0, 3.0], {"block": 1, "draws": 10}),
        ([1.0, 2.0, 3.0], {"block": 1, "level": 1.0}),
    ],
)
def test_block_bootstrap_rejects_inputs_it_cannot_interpret(values, options):
    with pytest.raises(ContractError):
        block_bootstrap_interval(values, seed=0, **options)


def scored_rows(scores_by_day, returns_by_day, session_days):
    """Archive one score per instrument per day, then evaluate against given returns.

    ``session_days`` lists the calendar length of each day's one-session horizon,
    for example 1 for a mid-week day and 3 for a Friday.
    """
    archive = ForecastArchive()
    # One continuous price path per instrument: each day's end price is the next
    # day's start price, so consecutive intervals never disagree about a timestamp.
    level = {instrument: 100.0 for instrument in INSTRUMENTS}
    prices = {(instrument, START): 100.0 for instrument in INSTRUMENTS}
    at = START
    for day, (scores, returns, length) in enumerate(
        zip(scores_by_day, returns_by_day, session_days)
    ):
        interval = Interval(at, at + timedelta(days=length))
        for instrument, score, realized in zip(INSTRUMENTS, scores, returns):
            # One signal node per instrument, as a real graph would be laid out.
            node = f"signal-{instrument.value}"
            view = ScoreView(
                return_target(instrument, interval),
                Score(score, "fixture"),
                Metadata(f"{node}:{day}", node, at, interval.end),
            )
            archive.record(node, NodeOutputs({"views": (view,)}), EventBatch(day, at))
            # Move the price so the simple return over the interval is `realized`.
            level[instrument] *= 1 + realized
            prices[instrument, interval.end] = level[instrument]
        at = interval.end

    def anchor(instrument, when, convention, as_of):
        price = prices.get((instrument, when))
        return None if price is None else Observation(when, when, price)

    return evaluate(archive, ReturnOutcomeResolver(anchor), as_of=at).rows


def test_cross_sectional_ic_ranks_instruments_within_each_date():
    scores = [(4, 3, 2, 1), (4, 3, 2, 1), (1, 2, 3, 4)]
    returns = [
        (0.04, 0.03, 0.02, 0.01),  # Day 0: perfectly ordered.
        (0.01, 0.02, 0.03, 0.04),  # Day 1: perfectly reversed.
        (0.01, 0.05, 0.02, 0.09),  # Day 2: mostly ordered.
    ]
    rows = scored_rows(scores, returns, session_days=(1, 1, 1))
    (cohort,) = cross_sectional_ic(rows)
    assert cohort["ic"] == pytest.approx((1.0, -1.0, 0.8))
    assert cohort["breadth"] == (4, 4, 4) and cohort["skipped"] == 0
    assert cohort["mean_ic"] == pytest.approx(0.8 / 3)
    assert cohort["times"] == (START, START + timedelta(days=1), START + timedelta(days=2))


def test_cross_sectional_ic_pools_session_horizons_and_skips_unrankable_dates():
    scores = [(4, 3, 2, 1), (4, 3, 2, 1), (2, 2, 2, 2)]
    returns = [(0.04, 0.03, 0.02, 0.01), (0.04, 0.03, 0.02, 0.01), (0.01, 0.02, 0.03, 0.04)]
    # The middle day is a "Friday": its one-session horizon lasts three calendar days.
    rows = scored_rows(scores, returns, session_days=(1, 3, 1))
    # Without pooling, the weekend-spanning date forms a cohort of its own.
    assert len(cross_sectional_ic(rows)) == 2
    (cohort,) = cross_sectional_ic(rows, pool_horizons=True)
    # The last date has identical scores for every instrument, so it has no
    # ranking to test and is counted as skipped rather than scored as zero.
    assert cohort["ic"] == pytest.approx((1.0, 1.0)) and cohort["skipped"] == 1
    with pytest.raises(ContractError, match="at least three"):
        cross_sectional_ic(rows, min_instruments=2)
