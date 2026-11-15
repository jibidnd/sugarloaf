"""A run's views scored against what prices then did, summarized by view kind and node.

``view_metrics(records, prices, as_of)`` resolves every archived view's target with
``ReturnOutcomeResolver`` over exact bar-end closes, scores it with the functions of
``sugarloaf.research.evaluation``, and summarizes the scored views pooled and per node:

* distribution views (a scalar ``Distribution``): pinball loss at the deciles, a PIT
  histogram, central 50% and 90% interval coverage, the log score where the law states a
  density, and realized outcomes grouped by forecast-median quintile;
* score views (``Score``, or an ``Estimate`` of the mean read as a score): hit rate,
  precision, recall and F1 with up as the positive class, and realized outcomes grouped by
  score decile with their quantiles;
* ranking views (``Ordering``): Kendall tau;
* probability views: Brier score and log loss.

Each group also carries its path: the mean realized return from signal time to each of
``PATH_STEPS`` evenly spaced times up to the target's end, from the last close at or before
each time. Every issued view is counted: scored, horizon after ``as_of``, no bar at an
anchor time, or unsupported. The QuantConnect launcher calls this at the end of a backtest,
when the clock is past every horizon it scores; any caller with a price table can.
"""

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from math import isfinite

import numpy as np

from sugarloaf.core.errors import CapabilityError
from sugarloaf.core.primitives import InstrumentId, Interval
from sugarloaf.core.views import Distribution, Estimate, Ordering, Probability, Score, Target
from sugarloaf.research.anchors import anchor_from_closes
from sugarloaf.research.archive import ForecastRecord
from sugarloaf.research.evaluation import (
    GROUP_QUANTILES,
    EvaluationRow,
    ReturnOutcomeResolver,
    _target_end,
    default_metrics,
    direction_metrics,
    evaluate,
    grouped_outcomes,
    pit_histogram,
    quantile_groups,
    quantile_metrics,
)

PATH_STEPS = 10
SCORE_GROUPS = 10
DISTRIBUTION_GROUPS = 5
SIGNIFICANT_DIGITS = 6
KINDS = ("distribution", "score", "ranking", "probability", "other")


class PriceTable:
    """Prices keyed by target convention, instrument and exact bar boundary (aware UTC).

    ``bars[convention][instrument]`` is a sequence of ``(end, close)`` pairs; ``opens``, of
    the same shape, holds ``(start, open)`` pairs. A target time at a bar end reads that
    bar's close; a time at a bar start with no close there reads the bar's open, which is
    how a view issued at the session open is scored. One table per convention, because one
    price series carries one adjustment: a price return reads split-adjusted prices, a
    total return prices adjusted for splits and dividends.
    """

    def __init__(
        self,
        bars: Mapping[str, Mapping[InstrumentId, Sequence[tuple[datetime, float]]]],
        opens: Mapping[str, Mapping[InstrumentId, Sequence[tuple[datetime, float]]]] | None = None,
    ):
        self._ends: dict[tuple[str, InstrumentId], list[datetime]] = {}
        self._closes: dict[tuple[str, InstrumentId], list[float]] = {}
        exact: dict[str, dict[InstrumentId, dict[datetime, float]]] = {}
        for convention, by_instrument in bars.items():
            for instrument, pairs in by_instrument.items():
                ordered = sorted(dict(pairs).items())
                self._ends[(convention, instrument)] = [end for end, _ in ordered]
                self._closes[(convention, instrument)] = [close for _, close in ordered]
                exact.setdefault(convention, {})[instrument] = dict(ordered)
        self._starts: set[tuple[str, InstrumentId, datetime]] = set()
        for convention, by_instrument in (opens or {}).items():
            for instrument, pairs in by_instrument.items():
                table = exact.setdefault(convention, {}).setdefault(instrument, {})
                for start, price in pairs:
                    if start not in table:
                        table[start] = price
                        self._starts.add((convention, instrument, start))
        anchors = {convention: anchor_from_closes(table) for convention, table in exact.items()}

        def anchor(instrument, at, convention, as_of):
            # A convention without a table has no price: the resolver reports it unavailable.
            chosen = anchors.get(convention)
            return chosen(instrument, at, convention, as_of) if chosen else None

        self.anchor = anchor

    def has(self, convention: str, instrument: InstrumentId, at: datetime) -> bool:
        """Whether a bar of ``instrument`` ends, or with an open recorded starts, at ``at``."""
        ends = self._ends.get((convention, instrument), [])
        index = bisect_right(ends, at)
        return (index > 0 and ends[index - 1] == at) or (convention, instrument, at) in self._starts

    def last(self, convention: str, instrument: InstrumentId, at: datetime) -> float | None:
        """The close of the last bar ending at or before ``at``; None when there is none."""
        key = (convention, instrument)
        index = bisect_right(self._ends.get(key, []), at)
        return self._closes[key][index - 1] if index else None


def view_kind(statement) -> str:
    """The metric family of a view's representation."""
    if isinstance(statement, Distribution):
        return "distribution"
    if isinstance(statement, Score) or (
        isinstance(statement, Estimate) and statement.property == "mean"
    ):
        return "score"
    if isinstance(statement, Ordering):
        return "ranking"
    if isinstance(statement, Probability):
        return "probability"
    return "other"


def score_view(view, outcome) -> dict:
    """``evaluate``'s metric function: the scores this module summarizes, for one view."""
    statement = view.statement
    kind = view_kind(statement)
    if kind == "score":
        value = statement.value
        realized = outcome.value
        if isinstance(realized, tuple):
            raise CapabilityError("A score view needs a scalar outcome")
        return {"score": float(value), "realized": float(realized)}
    if kind == "distribution":
        realized = outcome.value
        if isinstance(realized, tuple) or getattr(statement, "dimension", 1) != 1:
            # A joint law has no scalar quantiles or PIT; its log score is in default_metrics.
            return default_metrics(view, outcome)
        realized = float(realized)
        metrics: dict = {"realized": realized}
        if statement.supports("quantile"):
            metrics.update(quantile_metrics(statement, realized))
        if statement.supports("cdf"):
            metrics["pit"] = min(1.0, max(0.0, float(statement.cdf(realized))))
        if statement.supports("logpdf"):
            metrics["log_score"] = -float(statement.logpdf(realized))
        if len(metrics) == 1:
            raise CapabilityError("The law states no quantile, cdf or density")
        return metrics
    return default_metrics(view, outcome)


def _leaves(target: Target):
    """(instrument, interval, convention) of every price series a target's outcome reads."""
    if target.quantity == "difference":
        for component in target.components:
            yield from _leaves(component)
        return
    subjects = target.subject if isinstance(target.subject, tuple) else (target.subject,)
    times = target.time if isinstance(target.time, tuple) else (target.time,) * len(subjects)
    for instrument, interval in zip(subjects, times):
        if isinstance(instrument, InstrumentId) and isinstance(interval, Interval):
            yield instrument, interval, target.convention


def price_requests(records: Iterable[ForecastRecord]) -> dict:
    """``{(convention, instrument): (earliest start, latest end)}`` the views' outcomes read."""
    spans: dict = {}
    for record in records:
        for instrument, interval, convention in _leaves(record.view.target):
            key = (convention, instrument)
            start, end = spans.get(key, (interval.start, interval.end))
            spans[key] = (min(start, interval.start), max(end, interval.end))
    return spans


def _path(target: Target, prices: PriceTable, steps: int) -> list[float] | None:
    """Realized return from the target's start to ``k/steps`` of its span, k = 1..steps."""
    if target.quantity == "difference":
        left, right = (_path(c, prices, steps) for c in target.components)
        if left is None or right is None:
            return None
        return [a - b for a, b in zip(left, right)]
    if not isinstance(target.subject, InstrumentId) or not isinstance(target.time, Interval):
        return None
    instrument, interval, convention = target.subject, target.time, target.convention
    start = prices.last(convention, instrument, interval.start)
    if not start or start <= 0:
        return None
    span = interval.end - interval.start
    path = []
    for k in range(1, steps + 1):
        close = prices.last(convention, instrument, interval.start + span * k / steps)
        path.append(close / start - 1)
    return path


def _mean_path(paths: list) -> list[float] | None:
    paths = [p for p in paths if p is not None]
    if not paths:
        return None
    return [float(v) for v in np.mean(np.array(paths, dtype=float), axis=0)]


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return float(np.mean(values)) if values else None


def _summary(kind: str, rows: list[EvaluationRow], paths: dict) -> dict:
    """The metrics of one view kind over ``rows`` (all scored)."""
    metrics = [row.metrics for row in rows]
    item: dict = {"count": len(rows)}
    if kind == "score":
        scores = [m["score"] for m in metrics]
        realized = [m["realized"] for m in metrics]
        item.update(direction_metrics(scores, realized))
        groups = grouped_outcomes(scores, realized, SCORE_GROUPS, quantiles=GROUP_QUANTILES)
        assignment = quantile_groups(scores, SCORE_GROUPS)
        for group in groups:
            members = [r for r, g in zip(rows, assignment) if g == group["group"]]
            group["path"] = _mean_path([paths.get(id(r)) for r in members])
        item["deciles"] = groups
        if len(groups) > 1:
            item["decile_spread"] = groups[-1]["mean"] - groups[0]["mean"]
    elif kind == "distribution":
        scalar = [m for m in metrics if "realized" in m]
        quantiled = [m for m in scalar if "pinball_mean" in m]
        if quantiled:
            item["pinball"] = {
                name.removeprefix("pinball_"): _mean(m[name] for m in quantiled)
                for name in quantiled[0]
                if name.startswith("pinball_") and name != "pinball_mean"
            }
            item["pinball_mean"] = _mean(m["pinball_mean"] for m in quantiled)
            item["coverage_50"] = _mean(m["covered_0.5"] for m in quantiled)
            item["coverage_90"] = _mean(m["covered_0.9"] for m in quantiled)
            medians = [m["median"] for m in quantiled]
            realized = [m["realized"] for m in quantiled]
            groups = grouped_outcomes(medians, realized, DISTRIBUTION_GROUPS)
            quantiled_rows = [r for r in rows if "pinball_mean" in r.metrics]
            assignment = quantile_groups(medians, DISTRIBUTION_GROUPS)
            for group in groups:
                group["forecast_median_mean"] = group.pop("key_mean")
                members = [r for r, g in zip(quantiled_rows, assignment) if g == group["group"]]
                group["path"] = _mean_path([paths.get(id(r)) for r in members])
            item["quintiles"] = groups
        pits = [m["pit"] for m in scalar if "pit" in m]
        if pits:
            item["pit_histogram"] = pit_histogram(pits)
        scores = [m["log_score"] for m in metrics if m.get("log_score") is not None]
        if scores:
            finite_scores = [s for s in scores if isfinite(s)]
            item["log_score_count"] = len(scores)
            item["log_score_infinite"] = len(scores) - len(finite_scores)
            item["log_score_mean"] = _mean(finite_scores)
    elif kind == "ranking":
        taus = [m.get("kendall_tau_b") for m in metrics]
        item["kendall_tau_mean"] = _mean(taus)
        item["kendall_tau_count"] = sum(t is not None for t in taus)
    elif kind == "probability":
        item["brier_mean"] = _mean(m.get("brier") for m in metrics)
        item["log_loss_mean"] = _mean(m.get("log_loss") for m in metrics)
    return item


def _counts(rows: list[EvaluationRow], prices: PriceTable) -> dict:
    statuses = Counter(row.status for row in rows)
    counts = {
        "issued": len(rows),
        "scored": statuses.get("scored", 0),
        "horizon_after_end": statuses.get("pending", 0),
        "no_bar": statuses.get("unavailable", 0),
        "unsupported": statuses.get("unsupported", 0),
        "no_bar_at_start": 0,
        "no_bar_at_end": 0,
    }
    # The first and last target end of each unscored status, to locate the views.
    for status, name in (("pending", "horizon_after_end"), ("unavailable", "no_bar")):
        ends = [_target_end(r.forecast.view.target) for r in rows if r.status == status]
        if ends:
            counts[f"{name}_ends"] = [min(ends).isoformat(), max(ends).isoformat()]
    for row in rows:
        if row.status != "unavailable":
            continue
        leaves = list(_leaves(row.forecast.view.target))
        if any(not prices.has(c, i, t.start) for i, t, c in leaves):
            counts["no_bar_at_start"] += 1
        if any(not prices.has(c, i, t.end) for i, t, c in leaves):
            counts["no_bar_at_end"] += 1
    return counts


def view_metrics(
    records: Iterable[ForecastRecord], prices: PriceTable, as_of: datetime
) -> dict:
    """Score every record against ``prices`` at ``as_of`` and summarize by kind and node.

    Returns ``{"as_of", "path_fractions", "counts", "by_node_counts", "kinds"}`` where
    ``kinds[kind]`` is ``{"pooled": summary, "by_node": {node: summary}}`` for each view
    kind with scored views. Floats are rounded to six significant digits; an infinite
    value is the string ``"inf"`` or ``"-inf"``, so the result is standard JSON.
    """
    records = list(records)
    resolver = ReturnOutcomeResolver(prices.anchor, provenance={"source": "bar closes"})
    report = evaluate(records, resolver, as_of=as_of, metric_fn=score_view)
    rows = list(report.rows)
    paths = {
        id(row): _path(row.forecast.view.target, prices, PATH_STEPS)
        for row in rows
        if row.status == "scored"
    }
    by_node: dict[str, list[EvaluationRow]] = defaultdict(list)
    for row in rows:
        by_node[row.forecast.node_id].append(row)
    kinds: dict = {}
    for kind in KINDS:
        scored = [
            r for r in rows if r.status == "scored" and view_kind(r.forecast.view.statement) == kind
        ]
        if not scored:
            continue
        nodes: dict[str, list] = defaultdict(list)
        for row in scored:
            nodes[row.forecast.node_id].append(row)
        kinds[kind] = {
            "pooled": _summary(kind, scored, paths),
            "by_node": {node: _summary(kind, members, paths) for node, members in nodes.items()},
        }
    unsupported = Counter(r.reason for r in rows if r.status == "unsupported")
    result = {
        "as_of": as_of.isoformat(),
        "path_fractions": [k / PATH_STEPS for k in range(1, PATH_STEPS + 1)],
        "counts": _counts(rows, prices),
        "by_node_counts": {node: _counts(members, prices) for node, members in by_node.items()},
        "kinds": kinds,
        "unsupported_reasons": dict(unsupported.most_common(5)),
    }
    return compact(result)


def compact(value):
    """Round floats to six significant digits; infinities become ``"inf"``/``"-inf"``, NaN None."""
    if isinstance(value, float):
        if value != value:
            return None
        if not isfinite(value):
            return "inf" if value > 0 else "-inf"
        return float(f"{value:.{SIGNIFICANT_DIGITS}g}")
    if isinstance(value, dict):
        return {str(k): compact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [compact(v) for v in value]
    if isinstance(value, np.generic):
        return compact(value.item())
    return value


def reduced(metrics: dict, level: int) -> dict:
    """``metrics`` with less detail, to fit a size budget.

    Level 0 is everything. Level 1 drops the per-node groups (deciles, quintiles) and their
    paths, keeping each node's headline numbers. Level 2 keeps pooled summaries only.
    """
    if level == 0:
        return metrics
    result = dict(metrics, detail=("by_node_headline", "pooled")[min(level, 2) - 1])
    kinds = {}
    for kind, parts in metrics.get("kinds", {}).items():
        if level >= 2:
            kinds[kind] = {"pooled": parts["pooled"]}
            continue
        kinds[kind] = {
            "pooled": parts["pooled"],
            "by_node": {
                node: {k: v for k, v in summary.items() if k not in ("deciles", "quintiles")}
                for node, summary in parts["by_node"].items()
            },
        }
    result["kinds"] = kinds
    if level >= 2:
        result.pop("by_node_counts", None)
    return result


def headline(metrics: dict) -> dict[str, str]:
    """Runtime-statistic names and values: views issued and scored, and each kind's primary
    metrics (score: hit rate, F1, top-minus-bottom decile mean return; distribution: mean
    pinball loss, 90% coverage, mean log score; ranking: mean Kendall tau)."""
    counts = metrics.get("counts", {})
    result = {
        "Views issued": str(counts.get("issued", 0)),
        "Views scored": str(counts.get("scored", 0)),
    }
    pooled = {kind: parts["pooled"] for kind, parts in metrics.get("kinds", {}).items()}
    names = {
        "score": (("Score hit rate", "hit_rate"), ("Score F1", "f1"),
                  ("Score decile spread", "decile_spread")),
        "distribution": (("Distribution pinball", "pinball_mean"),
                         ("Distribution coverage 90", "coverage_90"),
                         ("Distribution log score", "log_score_mean")),
        "ranking": (("Ranking Kendall tau", "kendall_tau_mean"),),
        "probability": (("Probability Brier", "brier_mean"),),
    }
    for kind, pairs in names.items():
        for label, key in pairs:
            if kind in pooled and pooled[kind].get(key) is not None:
                result[label] = f"{pooled[kind][key]:.4g}" if isinstance(
                    pooled[kind][key], float) else str(pooled[kind][key])
    return result
