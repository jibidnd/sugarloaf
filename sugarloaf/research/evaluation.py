"""Causal outcome resolution and per-claim predictive diagnostics.

Outcome data belong only to this evaluation layer, never a strategy Context.
The metrics are descriptive: overlapping horizons and forecast revisions are
dependent observations, so no independent-sample significance tests are implied.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite, log
from typing import Callable, Iterable, Mapping, Protocol

import numpy as np
from scipy.stats import kendalltau, rankdata

from sugarloaf.core.errors import CapabilityError, ContractError, NotReady
from sugarloaf.core.primitives import InstrumentId, Interval, aware, finite, frozen_map
from sugarloaf.core.views import (
    Distribution,
    Estimate,
    Ordering,
    Probability,
    ReturnEvent,
    Score,
    Target,
    View,
)
from sugarloaf.market.contracts import Observation
from sugarloaf.research.archive import ForecastRecord


def _target_end(target: Target) -> datetime:
    # Joint targets mature only once every coordinate reaches its endpoint.
    times = target.time if isinstance(target.time, tuple) else (target.time,)
    return max(t.end if isinstance(t, Interval) else t for t in times)


@dataclass(frozen=True)
class Outcome:
    """An exact-target label, or an explicit reason it remains unresolved."""

    target: Target
    value: float | tuple[float, ...] | None = None
    available_at: datetime | None = None
    status: str = "resolved"
    reason: str = ""
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", frozen_map(self.provenance))
        if self.status not in {"resolved", "pending", "unavailable", "unsupported"}:
            raise ContractError("Unknown outcome status")
        # A label needs both a finite value and a causal availability timestamp.
        # Unresolved states below keep absence explicit instead of inventing zero.
        if self.status == "resolved":
            if self.value is None or self.available_at is None:
                raise ContractError("Resolved outcomes need values and availability")
            aware(self.available_at)
            values = self.value if isinstance(self.value, tuple) else (self.value,)
            if not values:
                raise ContractError("Resolved joint outcome cannot be empty")
            finite(*values)
            # Even a data source with early timestamps cannot make the completed
            # target outcome knowable before the target itself has ended.
            if self.available_at < _target_end(self.target):
                raise ContractError("Outcome cannot be available before its target ends")
        elif self.value is not None or self.available_at is not None or not self.reason:
            raise ContractError("Unresolved outcomes need a reason and no invented label")


class OutcomeResolver(Protocol):
    """Resolve one immutable target using only labels available by ``as_of``."""

    def resolve(self, target: Target, *, as_of: datetime) -> Outcome:
        """Resolve an exact target with labels available by as_of, or explain its absence."""
        ...


Anchor = Callable[[InstrumentId, datetime, str, datetime], Observation | None]


class ReturnOutcomeResolver:
    """Resolve exact simple returns using a separate, availability-aware label source.

    ``anchor(instrument, at, convention, as_of)`` returns an Observation at the
    exact requested time, or None. Its availability must be <= as_of. The source
    owns adjustment conventions; no nearest observation or price substitution is
    performed. Positive start values and nonnegative end values are required.
    """

    def __init__(self, anchor: Anchor, *, provenance: Mapping[str, str] | None = None) -> None:
        self.anchor = anchor
        self.provenance = frozen_map({"resolver": "exact-simple-return-1", **(provenance or {})})

    def resolve(self, target: Target, *, as_of: datetime) -> Outcome:
        """Resolve scalar, joint, or aligned difference returns from exact anchors.

        Args:
            target: Decimal price-return or total-return target.
            as_of: Aware evaluation availability boundary.

        Returns:
            Resolved label or pending, unavailable, or unsupported status with a reason.

        Raises:
            ContractError: Source substitutes an observation time or returns invalid prices.
        """
        aware(as_of)
        # Horizon maturity and label availability are separate. This early exit
        # avoids asking an anchor source for a target that has not ended yet.
        if _target_end(target) > as_of:
            return Outcome(
                target,
                status="pending",
                reason="Target horizon has not ended",
                provenance=self.provenance,
            )
        try:
            value, available_at = self._resolve(target, as_of)
        # Missing/delayed data is unavailable; unsupported target semantics are a
        # separate coverage category. Both remain visible in row-level evidence.
        except NotReady as exc:
            return Outcome(
                target, status="unavailable", reason=str(exc), provenance=self.provenance
            )
        except CapabilityError as exc:
            return Outcome(
                target, status="unsupported", reason=str(exc), provenance=self.provenance
            )
        return Outcome(target, value, available_at, provenance=self.provenance)

    def _resolve(
        self, target: Target, as_of: datetime
    ) -> tuple[float | tuple[float, ...], datetime]:
        # This resolver implements simple decimal returns only. Do not silently
        # reinterpret a score, price level, log return or another unit as a return.
        if target.units != "decimal" or target.convention not in {"price_return", "total_return"}:
            raise CapabilityError("Default outcomes require decimal price/total returns")
        if target.quantity == "difference":
            if len(target.components) != 2:
                raise CapabilityError("A difference outcome needs two return targets")
            if any(
                component.quantity != "return"
                or component.units != target.units
                or component.convention != target.convention
                or component.time != target.time
                for component in target.components
            ) or target.subject != tuple(component.subject for component in target.components):
                raise CapabilityError("Default difference outcomes need aligned return components")
            # A relative label requires two aligned scalar returns. Its availability
            # is the later component availability, not merely their common horizon.
            left, right = (self._resolve(component, as_of) for component in target.components)
            if isinstance(left[0], tuple) or isinstance(right[0], tuple):
                raise CapabilityError("Default difference outcomes require scalar components")
            return left[0] - right[0], max(left[1], right[1])
        if target.quantity != "return":
            raise CapabilityError(f"No default outcome for quantity {target.quantity}")
        # Preserve subject/interval coordinate order. A joint density or ranking
        # must receive its outcomes in the same order as the immutable target.
        joint = isinstance(target.subject, tuple)
        subjects = target.subject if joint else (target.subject,)
        intervals = (
            target.time if isinstance(target.time, tuple) else (target.time,) * len(subjects)
        )
        if len(subjects) != len(intervals):
            raise CapabilityError("Joint outcome requires one interval per coordinate")
        results: list[float] = []
        availability: list[datetime] = []
        for instrument, interval in zip(subjects, intervals):
            if not isinstance(instrument, InstrumentId) or not isinstance(interval, Interval):
                raise CapabilityError("Return outcomes need instrument interval targets")
            if interval.end > as_of:
                raise NotReady("A component horizon has not ended")
            observations = []
            # Ask for exact endpoints with the declared adjustment convention.
            # The anchor source owns its data vintage and point-in-time availability.
            for at in (interval.start, interval.end):
                observation = self.anchor(instrument, at, target.convention, as_of)
                if observation is None:
                    raise NotReady(
                        f"Exact {target.convention} anchor missing: {instrument.value} {at}"
                    )
                # Nearest-price fallback would change the forecast horizon. Treat
                # substituted times as a contract error, not a usable label.
                if observation.time != at:
                    raise ContractError("Outcome source substituted a different observation time")
                # Observation time alone is insufficient: a late publication or
                # revision cannot be used before this evaluation availability cutoff.
                if observation.available_at > as_of:
                    raise NotReady(f"Outcome anchor not yet available: {instrument.value} {at}")
                observations.append(observation)
            start, end = observations
            if start.value <= 0 or end.value < 0:
                raise ContractError("Return anchors require positive start and nonnegative end")
            # Simple return includes a zero end value as a complete loss; the start
            # must be positive. Availability must cover both endpoint observations.
            results.append(end.value / start.value - 1)
            availability.extend(observation.available_at for observation in observations)
        return (tuple(results) if joint else results[0]), max(availability)


def _scalar(outcome: Outcome) -> float:
    if isinstance(outcome.value, tuple) or outcome.value is None:
        raise CapabilityError("This predictive statement needs a scalar outcome")
    return float(outcome.value)


def _log_loss(probability: float) -> float:
    # An impossible observed event has infinite loss. Preserve that failure rather
    # than clipping probabilities into an arbitrary finite score.
    return -log(probability) if probability > 0 else float("inf")


def _event_occurred(event: ReturnEvent, value: float) -> bool:
    # Shared event semantics are lower-open and upper-closed. Boundary outcomes
    # therefore belong to only one bin of an exhaustive adjacent partition.
    return (event.lower is None or value > event.lower) and (
        event.upper is None or value <= event.upper
    )


def _probability_metrics(statement: Probability, value: float) -> dict[str, float]:
    indicators = [int(_event_occurred(event, value)) for event in statement.events]
    probabilities = statement.probabilities
    metrics = {}
    # Retain event-level predicted/observed pairs for later reliability summaries,
    # as well as the aggregate proper scoring losses computed below.
    for k, (probability, observed) in enumerate(zip(probabilities, indicators)):
        metrics[f"probability_{k}"] = probability
        metrics[f"observed_{k}"] = observed
    # Exhaustive partitions are one categorical forecast: sum Brier components
    # and score the probability of the one realized event against a uniform baseline.
    if statement.exhaustive:
        if sum(indicators) != 1:
            raise ContractError("Exhaustive outcomes did not select exactly one event")
        metrics["brier"] = sum((p - y) ** 2 for p, y in zip(probabilities, indicators))
        metrics["log_loss"] = _log_loss(probabilities[indicators.index(1)])
        metrics["baseline_brier"] = 1 - 1 / len(probabilities)
        metrics["baseline_log_loss"] = log(len(probabilities))
    else:
        # Separate events are binary claims, possibly overlapping. Average their
        # binary losses and compare with a 0.5 probability for each event.
        metrics["brier"] = float(np.mean([(p - y) ** 2 for p, y in zip(probabilities, indicators)]))
        metrics["log_loss"] = float(
            np.mean([_log_loss(p if y else 1 - p) for p, y in zip(probabilities, indicators)])
        )
        metrics["baseline_brier"] = 0.25
        metrics["baseline_log_loss"] = log(2)
    return metrics


def _ranking_metrics(view: View, outcome: Outcome) -> dict[str, float | None]:
    if not isinstance(outcome.value, tuple) or len(outcome.value) != len(view.target.subject):
        raise CapabilityError("Ranking outcome must follow its target's instrument order")
    # Resolve values by stable instrument identity before comparing predicted
    # groups. Group order expresses preference; within-group order expresses no rank.
    realized = dict(zip(view.target.subject, outcome.value))
    groups = view.statement.groups
    predicted_ranks = [-k for k, group in enumerate(groups) for _ in group]
    tau = kendalltau(predicted_ranks, outcome.value).statistic
    # Score only strictly ordered predicted pairs. A realized tie earns half credit
    # rather than being counted as either a correct or incorrect strict ordering.
    comparisons = [
        float(realized[a] > realized[b]) + 0.5 * (realized[a] == realized[b])
        for k, group in enumerate(groups)
        for later in groups[k + 1 :]
        for a in group
        for b in later
    ]
    # Report dispersion within predicted ties separately; it is not an ordered
    # pair forecast and should not inflate strict-pair concordance counts.
    tie_spreads = [
        abs(realized[a] - realized[b])
        for group in groups
        for k, a in enumerate(group)
        for b in group[k + 1 :]
    ]
    # This is a descriptive difference of realized group means, not portfolio P&L:
    # no allocation, transaction costs or execution assumptions enter this statistic.
    spread = np.mean([realized[i] for i in groups[0]]) - np.mean([realized[i] for i in groups[-1]])
    return {
        "kendall_tau_b": float(tau) if isfinite(tau) else None,
        "pair_concordance": float(np.mean(comparisons)) if comparisons else None,
        "top_bottom_return_spread": float(spread),
        "within_tie_realized_spread": float(np.mean(tie_spreads)) if tie_spreads else None,
        "random_order_baseline_concordance": 0.5 if comparisons else None,
    }


def _distribution_metrics(view: View, outcome: Outcome) -> dict[str, float]:
    law = view.statement
    # Joint predictions must be scored at their full realized coordinate vector.
    # Multiplying marginal densities would silently discard the stated dependence.
    dimension = len(outcome.value) if isinstance(outcome.value, tuple) else 1
    if getattr(law, "dimension", dimension) != dimension:
        raise ContractError("Predictive law dimension differs from the realized outcome")
    # Discrete event-mass laws use categorical proper scores rather than treating
    # an event probability as a continuous density in arbitrary return units.
    if hasattr(law, "events") and hasattr(law, "masses"):
        return _probability_metrics(
            Probability(tuple(law.events), tuple(law.masses), exhaustive=True), _scalar(outcome)
        )
    if not law.supports("pdf"):
        raise CapabilityError(
            "Distribution scoring requires a joint/scalar density or event masses"
        )
    # Log density is used for numerical stability. Continuous densities can exceed
    # one, so negative log scores can be negative and depend on units/dimension.
    log_density = float(law.logpdf(outcome.value))
    if np.isnan(log_density) or log_density == float("inf"):
        raise ContractError("Predictive distribution returned an invalid log density")
    score = -log_density
    metrics = {"log_score": score}
    # Scalar PIT describes where the realization falls in the predicted CDF.
    # Do not invent a scalar CDF transform for a joint distribution.
    if not isinstance(outcome.value, tuple) and law.supports("cdf"):
        pit = float(law.cdf(outcome.value))
        if not isfinite(pit) or not 0 <= pit <= 1:
            raise ContractError("Predictive law returned an invalid CDF")
        metrics["pit"] = pit
    return metrics


def default_metrics(view: View, outcome: Outcome) -> Mapping[str, float | None]:
    """Score an issued predictive claim without fitting or calibrating it.

    Args:
        view: Supported predictive View with explicit target semantics.
        outcome: Resolved scalar or joint label for that exact target.

    Returns:
        Named descriptive metrics; undefined metrics use None, infinite losses remain.

    Raises:
        ContractError: Outcome alignment or predictive density is malformed.
        CapabilityError: View or outcome form has no supported default metric.

    Errors are predicted minus realized. Ranking ties receive half credit.
    Distribution scores use the joint density; raw scores are not return estimates.
    """
    if outcome.status != "resolved" or outcome.target != view.target:
        raise ContractError("Metrics require a resolved outcome for the exact View target")
    # Metrics follow the representation, whatever named View class carries it.
    statement = view.statement
    if isinstance(statement, Estimate) and statement.property == "mean":
        realized = _scalar(outcome)
        # Error direction is predicted minus realized. Standardization uses the
        # claim's stated forecast-error scale; it does not fit calibration here.
        error = view.statement.value - realized
        return {
            "error": error,
            "absolute_error": abs(error),
            "squared_error": error**2,
            "standardized_error": error / view.statement.forecast_error_scale,
            "zero_baseline_squared_error": realized**2,
        }
    if isinstance(statement, Probability):
        return _probability_metrics(view.statement, _scalar(outcome))
    # Raw scores stay raw. Association is summarized across comparable forecasts;
    # no default mapping turns an arbitrary score into a calibrated return estimate.
    if isinstance(statement, Score):
        return {"score": view.statement.value, "realized": _scalar(outcome)}
    if isinstance(statement, Ordering):
        return _ranking_metrics(view, outcome)
    if isinstance(statement, Distribution):
        return _distribution_metrics(view, outcome)
    raise CapabilityError(f"No default predictive metrics for {type(view).__name__}")


def _target_group(target: Target, issued_at: datetime, pool_horizons: bool = False) -> tuple:
    """Keep horizon and available lead time while pooling distinct calendar dates."""
    times = target.time if isinstance(target.time, tuple) else (target.time,)
    # Pool dates only when full horizon and receipt-to-end lead time agree.
    # A revision closer to maturity is a different predictive information setting.
    #
    # pool_horizons drops that timing from the group. It exists for horizons
    # counted in trading sessions: "one session" is one calendar day mid-week but
    # three over a weekend, and exact timing would split one daily signal into a
    # separate cohort for each of those lengths.
    timing = () if pool_horizons else tuple(
        ((time.end - time.start).total_seconds(), (time.end - issued_at).total_seconds())
        if isinstance(time, Interval)
        else (None, (time - issued_at).total_seconds())
        for time in times
    )
    return (
        target.subject,
        target.quantity,
        target.units,
        target.convention,
        timing,
        tuple(_target_group(t, issued_at, pool_horizons) for t in target.components),
    )


def _cohort(record: ForecastRecord, pool_horizons: bool = False) -> tuple:
    view = record.view
    statement = view.statement
    # Model/target semantics alone do not identify calibration or event meanings.
    # Preserve those distinctions so incompatible predictive claims do not pool.
    detail = ()
    if isinstance(statement, Score):
        detail = (statement.calibration_id,)
    elif isinstance(statement, Estimate):
        detail = (statement.property, statement.level)
    elif isinstance(statement, Probability):
        detail = (statement.events, statement.exhaustive)
    elif isinstance(statement, Distribution):
        detail = (
            type(statement).__module__,
            type(statement).__qualname__,
            getattr(statement, "events", ()),
        )
    target_group = _target_group(view.target, record.received_at, pool_horizons)
    if isinstance(statement, Ordering):
        # Cohorts describe the same ranked universe, not the forecast's ordering.
        target_group = (tuple(sorted(view.target.subject)), *target_group[1:])
    return (
        # Cohorts follow the representation, not the named View class carrying it.
        type(view.statement).__name__,
        record.node_id,
        view.metadata.model_version,
        target_group,
        detail,
    )


@dataclass(frozen=True)
class EvaluationRow:
    """One archived forecast, its resolved/unresolved outcome, and diagnostics."""

    forecast: ForecastRecord
    outcome: Outcome
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    status: str = "scored"
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", frozen_map(self.metrics))

    @property
    def issued_after_target_end(self) -> bool:
        """Flag retrospective receipt at or after the forecast target ends."""
        return self.forecast.received_at >= _target_end(self.forecast.view.target)


def _score_correlations(rows: list[EvaluationRow]) -> dict[str, float | None]:
    """Describe score association only when both series contain variation."""
    item = {}
    scores = np.array([row.metrics.get("score", np.nan) for row in rows], dtype=float)
    realized = np.array([row.metrics.get("realized", np.nan) for row in rows], dtype=float)
    # At least three finite, nonconstant pairs are required for descriptive
    # correlation. Undefined association stays None; no significance test is implied.
    valid = (
        len(rows) >= 3
        and np.all(np.isfinite(scores))
        and np.all(np.isfinite(realized))
        and np.ptp(scores) > 0
        and np.ptp(realized) > 0
    )
    item["pearson_correlation"] = float(np.corrcoef(scores, realized)[0, 1]) if valid else None
    item["spearman_correlation"] = (
        float(np.corrcoef(rankdata(scores), rankdata(realized))[0, 1]) if valid else None
    )
    return item


def _probability_calibration(rows: list[EvaluationRow], names: set[str]) -> dict:
    """Reliability bins retain the event definition supplied by the cohort."""
    calibration = {}
    for name in names:
        if not isinstance(
            rows[0].forecast.view.statement, (Probability, Distribution)
        ) or not name.startswith("probability_"):
            continue
        suffix = name.removeprefix("probability_")
        bins = defaultdict(list)
        for row in rows:
            p, y = row.metrics.get(name), row.metrics.get(f"observed_{suffix}")
            if p is not None and y is not None:
                if not 0 <= p <= 1 or y not in (0, 1):
                    raise ContractError(
                        "Calibration metrics need probabilities and binary outcomes"
                    )
                # Ten bins include probability 1 in the final bin. Keep each event
                # definition separate through its surrounding cohort identity.
                bins[min(9, int(p * 10))].append((p, y))
        calibration[suffix] = tuple(
            {
                "lower": k / 10,
                "upper": (k + 1) / 10,
                "count": len(values),
                "mean_probability": float(np.mean([v[0] for v in values])),
                "event_frequency": float(np.mean([v[1] for v in values])),
            }
            for k, values in sorted(bins.items())
        )
    return calibration


@dataclass(frozen=True)
class EvaluationReport:
    """Row-level evidence plus descriptive summaries at one evaluation cutoff."""

    rows: tuple[EvaluationRow, ...]
    as_of: datetime

    @property
    def coverage(self) -> dict:
        """Report issued/scored counts, unresolved statuses, and retrospective forecasts."""
        # Report the full archive coverage, including pending/unavailable/unsupported
        # rows. A high score on a small resolved subset must not hide missing labels.
        counts = Counter(row.status for row in self.rows)
        return {
            "issued": len(self.rows),
            "scored": counts.get("scored", 0),
            "by_status": dict(counts),
            "issued_after_target_end": sum(row.issued_after_target_end for row in self.rows),
        }

    def summary(self, *, pool_horizons: bool = False) -> tuple[dict, ...]:
        """Descriptive cohorts, preserving units, horizons, model and issue-time lead.

        Args:
            pool_horizons: Group forecasts regardless of their calendar length and
                lead time. Use it for horizons counted in trading sessions, whose
                calendar length varies (a Friday forecast spans the weekend). By
                default only forecasts with identical timing share a cohort.

        Revisions with different remaining horizons form different cohorts. No
        standard errors/p-values treat overlapping forecasts as independent.
        Infinite log losses remain infinite; they are not dropped from averages.

        For an interval around one of these numbers, or for a per-date rank
        correlation across instruments, see ``sugarloaf.research.inference``.
        """
        # Summaries use scored rows only; coverage separately retains every issued
        # claim and unresolved reason. Cohorts preserve model and target semantics.
        groups = defaultdict(list)
        for row in self.rows:
            if row.status == "scored":
                groups[_cohort(row.forecast, pool_horizons)].append(row)
        result = []
        for cohort, rows in groups.items():
            # Each metric averages its defined values. Undefined values remain in
            # the original rows, and infinite losses are not silently filtered out.
            names = set().union(*(row.metrics for row in rows))
            means = {}
            for name in sorted(names):
                values = [row.metrics[name] for row in rows if row.metrics.get(name) is not None]
                # Opposite infinities have no defined average; preserve the row
                # values and report an undefined aggregate rather than a NaN.
                mixed_infinity = float("inf") in values and float("-inf") in values
                means[name] = float(np.mean(values)) if values and not mixed_infinity else None
            # Forecast revisions and overlapping horizons are dependent evidence.
            # Distinct-target counts describe that reuse without claiming independence.
            item = {
                "cohort": cohort,
                "count": len(rows),
                "means": means,
                "distinct_targets": len({row.forecast.view.target for row in rows}),
                "infinite_scores": sum(
                    any(value is not None and not isfinite(value) for value in row.metrics.values())
                    for row in rows
                ),
                "issued_after_target_end": sum(row.issued_after_target_end for row in rows),
            }
            if isinstance(rows[0].forecast.view.statement, Score):
                item.update(_score_correlations(rows))
            calibration = _probability_calibration(rows, names)
            if calibration:
                item["calibration"] = calibration
            result.append(item)
        return tuple(result)


def evaluate(
    archive: Iterable[ForecastRecord],
    resolver: OutcomeResolver,
    *,
    as_of: datetime,
    metric_fn: Callable[[View, Outcome], Mapping[str, float | None]] = default_metrics,
) -> EvaluationReport:
    """Resolve and score archived forecasts at an explicit availability boundary.

    Args:
        archive: Issued forecast records, including replaced or expired Views.
        resolver: Exact-target label source independent of strategy inputs.
        as_of: Aware time limiting both forecast issuance and label availability.
        metric_fn: Explicit scoring hook; may extend default_metrics.

    Returns:
        Row-level outcomes, metrics, and unresolved reasons, plus descriptive summaries.

    Raises:
        ContractError: Resolver changes a target, leaks a future label, or metrics
            contain invalid names or NaN.

    Unsupported metrics remain unscored rows. Exact targets share one resolved
    label per evaluation. Calibration must be supplied causally by the experiment.
    """
    aware(as_of)
    rows: list[EvaluationRow] = []
    # Labels are evaluation-owned and never enter the node/provider Context.
    # Cache one resolution per exact immutable target within this evaluation only.
    outcomes: dict[Target, Outcome] = {}
    for record in archive:
        # Backdated metadata does not make a later-issued forecast available at
        # an earlier evaluation cutoff. Use its archived first receipt timestamp.
        if record.received_at > as_of:
            outcome = Outcome(
                record.view.target,
                status="pending",
                reason="Forecast had not been issued by evaluation as_of",
            )
        else:
            target = record.view.target
            # Revisions of one exact target share a label, not a new observation.
            if target not in outcomes:
                outcomes[target] = resolver.resolve(target, as_of=as_of)
            outcome = outcomes[target]
        # Extension resolvers remain subject to exact-target and availability
        # contracts; custom implementations cannot silently weaken causality.
        if not isinstance(outcome, Outcome):
            raise ContractError("Outcome resolver must return Outcome")
        if outcome.target != record.view.target:
            raise ContractError("Outcome resolver changed the forecast target")
        # Keep an unscored row with its explicit missing-outcome reason. Dropping
        # it would overstate coverage and conceal the selection of scorable labels.
        if outcome.status != "resolved":
            rows.append(
                EvaluationRow(record, outcome, status=outcome.status, reason=outcome.reason)
            )
            continue
        if outcome.available_at > as_of:
            raise ContractError("Outcome resolver leaked a label unavailable at evaluation as_of")
        try:
            metrics = metric_fn(record.view, outcome)
        except CapabilityError as exc:
            # A resolved label may still have no applicable metric. Keep that
            # distinction visible rather than inventing an unrelated loss function.
            rows.append(EvaluationRow(record, outcome, status="unsupported", reason=str(exc)))
            continue
        # Normalize metric scalars while preserving None and meaningful infinities.
        # NaN is ambiguous missingness and must be represented explicitly as None.
        cleaned = {}
        for name, value in metrics.items():
            if not isinstance(name, str) or not name:
                raise ContractError("Metric names must be nonempty strings")
            if value is not None:
                value = float(value)
                if np.isnan(value):
                    raise ContractError("Undefined metrics must use None, not NaN")
            cleaned[name] = value
        rows.append(EvaluationRow(record, outcome, cleaned))
    return EvaluationReport(tuple(rows), as_of)
