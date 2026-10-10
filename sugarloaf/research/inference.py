"""Uncertainty and cross-sectional summaries for evaluated forecasts.

``evaluation`` scores each forecast and averages those scores. Two questions it
leaves open are answered here:

* *How uncertain is a summary number?* ``block_bootstrap_interval`` gives an
  interval that respects the serial dependence in forecast data.
* *Does the signal rank instruments correctly at each point in time?*
  ``cross_sectional_ic`` computes one rank correlation per decision time, the
  standard "information coefficient" for a cross-sectional signal.

Neither function decides whether a signal is tradable. Both describe predictive
association before costs, capacity and portfolio construction.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable

import numpy as np
from numpy.typing import ArrayLike
from scipy.stats import rankdata

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, Interval
from sugarloaf.core.views import ReturnView, ScoreView
from sugarloaf.research.evaluation import EvaluationRow


def block_bootstrap_interval(
    values: ArrayLike,
    *,
    block: int,
    seed: int,
    statistic: Callable[[np.ndarray], float] = np.mean,
    draws: int = 2000,
    level: float = 0.95,
) -> dict:
    """Estimate a confidence interval for a statistic of a time-ordered series.

    Args:
        values: Observations in time order: shape ``(T,)``, or ``(T, k)`` when
            several series must be resampled together on the same dates (for
            example two strategies' returns, to compare them).
        block: Number of consecutive observations resampled as one unit.
        seed: Random seed. It is required so a reported interval is reproducible.
        statistic: Function of a resampled array returning one number.
        draws: Number of bootstrap resamples.
        level: Central coverage of the interval, such as 0.95.

    Returns:
        ``estimate`` (the statistic on the original data), ``low`` and ``high``
        (the percentile interval), plus the settings used.

    Raises:
        ContractError: Inputs are empty, non-finite, or inconsistent, or the
            statistic is not a finite number on some resample.

    Why blocks: an ordinary bootstrap resamples single observations, which is
    only right when they are independent. Forecast data rarely are. A 20-day
    forecast issued every day shares 19 days of its outcome with the next one,
    so 250 such forecasts carry far less information than 250 independent ones.
    Resampling whole blocks keeps that dependence inside each block and so
    produces an honestly wider interval.

    Choosing ``block``: make it at least as long as the dependence. For
    overlapping h-period forecasts issued every period, use at least h. For
    monthly decisions, three or more months is a common choice. A block of 1 is
    the ordinary bootstrap and claims the observations are independent.

    This is the circular moving-block bootstrap: a block that runs off the end
    of the series wraps around to the start, so every observation is equally
    likely to be drawn.
    """
    data = np.asarray(values, dtype=float)
    if data.ndim not in (1, 2) or len(data) < 2 or not np.all(np.isfinite(data)):
        raise ContractError("Bootstrap needs at least two finite time-ordered observations")
    count = len(data)
    if isinstance(block, bool) or not isinstance(block, int) or not 1 <= block <= count:
        raise ContractError("Block length must be an integer between 1 and the sample size")
    if isinstance(draws, bool) or not isinstance(draws, int) or draws < 100:
        raise ContractError("Use at least 100 bootstrap draws")
    if not 0 < level < 1:
        raise ContractError("Interval level must lie strictly between 0 and 1")

    generator = np.random.default_rng(seed)
    # Enough blocks to cover the original length; the surplus is trimmed below so
    # every resample has exactly as many observations as the data.
    blocks_per_draw = -(-count // block)  # Ceiling division.
    offsets = np.arange(block)
    estimates = np.empty(draws)
    for draw in range(draws):
        starts = generator.integers(0, count, size=blocks_per_draw)
        # Each row is one block of consecutive positions, wrapping past the end.
        positions = ((starts[:, None] + offsets[None, :]) % count).ravel()[:count]
        estimates[draw] = statistic(data[positions])
    estimate = float(statistic(data))
    if not np.isfinite(estimate) or not np.all(np.isfinite(estimates)):
        # A NaN here usually means the statistic is undefined on some resample,
        # such as a correlation computed on a constant block.
        raise ContractError("Statistic was not a finite number on the data or a resample")
    tail = (1 - level) / 2
    low, high = np.quantile(estimates, [tail, 1 - tail])
    return {
        "estimate": estimate,
        "low": float(low),
        "high": float(high),
        "level": level,
        "block": block,
        "draws": draws,
        "observations": count,
        "seed": seed,
    }


def _cross_section_key(row: EvaluationRow, pool_horizons: bool) -> tuple | None:
    """Return the cohort a row's forecast belongs to, or None when it cannot be ranked."""
    view = row.forecast.view
    if row.status != "scored" or not isinstance(view, (ScoreView, ReturnView)):
        return None
    target = view.target
    # A cross-section ranks single instruments over one shared interval, against
    # one realized number each. Joint or relative claims have no such ranking.
    if (
        not isinstance(target.subject, InstrumentId)
        or not isinstance(target.time, Interval)
        or isinstance(row.outcome.value, tuple)
        or row.outcome.value is None
    ):
        return None
    # Scores under different calibration identities mean different things and
    # must not be ranked against each other.
    meaning = view.statement.calibration_id if isinstance(view, ScoreView) else None
    duration = None if pool_horizons else (target.time.end - target.time.start).total_seconds()
    return (
        type(view).__name__,
        view.metadata.model_version,
        meaning,
        target.quantity,
        target.units,
        target.convention,
        duration,
    )


def cross_sectional_ic(
    rows: Iterable[EvaluationRow],
    *,
    min_instruments: int = 3,
    pool_horizons: bool = False,
) -> tuple[dict, ...]:
    """Rank-correlate predictions with outcomes across instruments at each decision time.

    Args:
        rows: Evaluated forecasts, usually ``EvaluationReport.rows``.
        min_instruments: Smallest cross-section worth ranking.
        pool_horizons: Treat forecasts of different calendar length as one
            cohort. Set it for session-count horizons: "one session" spans one
            day mid-week and three days over a weekend, and without pooling
            those would be reported as separate cohorts.

    Returns:
        One summary per cohort, each with:

        * ``times``: the start of each scored cross-section, in order;
        * ``ic``: that cross-section's Spearman rank correlation;
        * ``breadth``: how many instruments it ranked;
        * ``mean_ic``: the average of ``ic``, or None when nothing was scored;
        * ``skipped``: cross-sections that were too small, had no variation in
            the predictions or outcomes, or held two forecasts for one instrument.

    A cross-section is every forecast in a cohort sharing one exact target
    interval. Forecasts may come from different nodes, which is the usual
    layout: one signal node per instrument.

    How to read it: an IC of 0.05 means that, on a typical date, instruments
    predicted to do better did slightly better. The per-date series is the
    evidence. Pass ``ic`` to ``block_bootstrap_interval`` for an interval around
    its mean, with a block at least as long as the forecast horizon in periods.

    This differs from the per-instrument correlations in
    ``EvaluationReport.summary``. Those ask whether one instrument's score
    predicts that instrument's return through time. This asks whether the score
    sorts instruments correctly against each other on a given date, which is what
    a long-short or ranking portfolio depends on.
    """
    if isinstance(min_instruments, bool) or not isinstance(min_instruments, int):
        raise ContractError("Minimum cross-section size must be an integer")
    if min_instruments < 3:
        # With two instruments a rank correlation can only be +1 or -1.
        raise ContractError("A cross-section needs at least three instruments to rank")

    # cohort -> exact interval -> list of (instrument, prediction, realized).
    sections: dict[tuple, dict[Interval, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        cohort = _cross_section_key(row, pool_horizons)
        if cohort is None:
            continue
        view = row.forecast.view
        sections[cohort][view.target.time].append(
            (view.target.subject, float(view.statement.value), float(row.outcome.value))
        )

    result = []
    for cohort, by_interval in sections.items():
        times, correlations, breadth, skipped = [], [], [], 0
        for interval in sorted(by_interval, key=lambda item: (item.start, item.end)):
            members = by_interval[interval]
            instruments = [instrument for instrument, _, _ in members]
            predicted = np.array([prediction for _, prediction, _ in members])
            realized = np.array([outcome for _, _, outcome in members])
            if (
                len(members) < min_instruments
                # Two forecasts for one instrument (a revision, or two nodes) would
                # count that instrument twice; which one to rank is not ours to guess.
                or len(set(instruments)) != len(instruments)
                # A rank correlation is undefined when either side is constant.
                or np.ptp(predicted) == 0
                or np.ptp(realized) == 0
            ):
                skipped += 1
                continue
            # Spearman correlation is the Pearson correlation of the ranks. Ranks
            # make it insensitive to one extreme return dominating the date.
            correlation = np.corrcoef(rankdata(predicted), rankdata(realized))[0, 1]
            times.append(interval.start)
            correlations.append(float(correlation))
            breadth.append(len(members))
        result.append(
            {
                "cohort": cohort,
                "times": tuple(times),
                "ic": tuple(correlations),
                "breadth": tuple(breadth),
                "mean_ic": float(np.mean(correlations)) if correlations else None,
                "skipped": skipped,
            }
        )
    return tuple(result)
