"""Naive, explicit baselines for combining forecasts and producing long/cash targets.

These rules fit no model, calibrate no scores, and optimize no portfolio.
Use separate position branches and PositionNetter for incompatible claim families.
"""

# Group forecasts without inventing a strategy registry or model-specific aggregation layer.
# Arithmetic means are deliberate baselines; no covariance or confidence gain is inferred.
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from math import isfinite
from statistics import fmean

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import InputSpec, NodeOutputs, OutputSpec
from sugarloaf.core.types import InstrumentId, Interval, Metadata
from sugarloaf.core.views import PositionView, ProbabilityView, ReturnEvent, ReturnView, ScoreView

SUPPORTED = (ReturnView, ScoreView, ProbabilityView)


def _current_views(inputs, ctx):
    views = inputs.by_port.get("views", ())
    InputSpec(SUPPORTED).validate(views)
    # Duplicate identity represents repeated evidence, not another independent forecast.
    if len({v.metadata.view_id for v in views}) != len(views):
        raise ContractError("Duplicate forecast identity would double-count evidence")
    for view in views:
        # A claim must be currently available and still concern an unresolved future interval.
        if not view.metadata.cutoff <= ctx.now < view.metadata.expires_at:
            raise NotReady("Forecast is unavailable or expired")
        if not isinstance(view.target.time, Interval) or view.target.time.end <= ctx.now:
            raise NotReady("Forecast needs an unresolved interval target")
    return views


def _metadata(views, ctx, identity, lifetime):
    # Derived authority is bounded by policy, every parent expiry, and every target end.
    # Parent IDs retain provenance even after runtime replaces the upstream active snapshot.
    return Metadata(
        identity,
        ctx.node_id,
        ctx.now,
        min(
            ctx.now + lifetime,
            *(v.metadata.expires_at for v in views),
            *(v.target.time.end for v in views),
        ),
        tuple(v.metadata.view_id for v in views),
    )


class MeanViewAggregator:
    """Average forecasts within exactly compatible targets and statements.

    Args:
        lifetime (timedelta): Maximum aggregate lifetime, bounded by parent expiry and target end.

    Score calibration IDs and probability event partitions must match. Return
    error scale remains the largest input scale; no independence or confidence
    improvement is assumed. Incompatible groups stay separate.
    """

    inputs = {"views": InputSpec(SUPPORTED)}
    outputs = {"views": OutputSpec(SUPPORTED)}
    requirements = ()
    # Upstream message changes activate this node; it needs no direct market subscription.
    triggers = ()

    def __init__(self, lifetime=timedelta(days=1)):
        if lifetime <= timedelta(0):
            raise ContractError("Positive aggregate lifetime required")
        self.lifetime = lifetime

    def evaluate(self, inputs, ctx):
        """Average compatible claims independently and emit fresh parent-linked Views."""
        groups = defaultdict(list)
        for view in _current_views(inputs, ctx):
            detail = None
            if isinstance(view, ScoreView):
                # A zero/scale from one calibration cannot be averaged with another meaning.
                detail = view.statement.calibration_id
            elif isinstance(view, ProbabilityView):
                # Probabilities align by ordered event definitions and partition semantics.
                detail = (view.statement.events, view.statement.exhaustive)
            # Exact type and Target preserve subject, units, return convention and horizon.
            groups[type(view), view.target, detail].append(view)
        outputs = []
        for index, views in enumerate(groups.values()):
            first = views[0]
            if isinstance(first, ProbabilityView):
                # Average each matching event separately; do not reorder or relabel categories.
                statement = replace(
                    first.statement,
                    probabilities=tuple(
                        fmean(v.statement.probabilities[k] for v in views)
                        for k in range(len(first.statement.events))
                    ),
                )
            else:
                statement = replace(first.statement, value=fmean(v.statement.value for v in views))
                if isinstance(first, ReturnView):
                    # Retain the largest reported error scale without claiming independence.
                    statement = replace(
                        statement,
                        forecast_error_scale=max(v.statement.forecast_error_scale for v in views),
                    )
            metadata = _metadata(
                views, ctx, f"{ctx.node_id}:{ctx.snapshot_id}:{index}", self.lifetime
            )
            # Reuse only the compatible target/statement structure, with fresh issuance identity.
            outputs.append(replace(first, statement=statement, metadata=metadata))
        return NodeOutputs({"views": tuple(outputs)})


class PositiveSignalPositions:
    """Allocate equal account-NAV weights to positive signals, with explicit zeros.

    Args:
        instruments (Sequence[InstrumentId]): Unique instruments requiring one aligned claim each.
        budget (float): Total account-NAV fraction in (0, 1], divided among passing signals.
        scope (str): Output branch or account identity.
        lifetime (timedelta): Maximum target lifetime, bounded by forecasts and target end.

    Return/score > 0 or P(return > 0) > 0.5 selects an instrument. Missing evidence
    withdraws authority without liquidating. The score's zero must have the intended
    meaning; active weight targets may cause maintenance trades.
    """

    inputs = {"views": InputSpec(SUPPORTED)}
    outputs = {"positions": OutputSpec((PositionView,))}
    requirements = ()
    triggers = ()

    def __init__(self, instruments, budget=0.9, scope="account", lifetime=timedelta(days=1)):
        self.instruments = tuple(instruments)
        # One configured identity per asset prevents an accidental double allocation.
        if (
            not self.instruments
            or len(set(self.instruments)) != len(self.instruments)
            or not all(isinstance(i, InstrumentId) for i in self.instruments)
        ):
            raise ContractError("Unique configured instrument identities required")
        if not isfinite(budget) or not 0 < budget <= 1 or not scope or lifetime <= timedelta(0):
            raise ContractError("Invalid position budget, scope or lifetime")
        self.budget, self.scope, self.lifetime = budget, scope, lifetime

    def evaluate(self, inputs, ctx):
        """Allocate the budget equally across passing signals and emit zeros elsewhere.

        Raises:
            NotReady: A configured instrument lacks a current forecast.
            ContractError: Claims are duplicated, unaligned, or lack required semantics.
        """
        views = _current_views(inputs, ctx)
        by_instrument = {}
        for view in views:
            target = view.target
            # This baseline needs scalar decimal-return targets, including score/probability claims.
            if (
                target.subject not in self.instruments
                or target.quantity != "return"
                or target.units != "decimal"
            ):
                raise ContractError("Position conversion requires configured scalar return targets")
            if target.subject in by_instrument:
                # Resolve competing claims explicitly before converting them to positions.
                raise ContractError(
                    "Aggregate to one claim per instrument before position conversion"
                )
            by_instrument[target.subject] = view
        if set(by_instrument) != set(self.instruments):
            # Unavailable evidence withdraws output; it is not an implicit cash instruction.
            raise NotReady("Missing instrument forecasts; no implicit liquidation")
        if len({(v.target.time, v.target.convention) for v in views}) != 1:
            # Cross-asset selection must compare the same forecast period and return definition.
            raise ContractError("Position signals must share a horizon and return convention")
        passing = set()
        for instrument, view in by_instrument.items():
            if isinstance(view, ProbabilityView):
                events = view.statement.events
                # Use an explicit positive-return event, not the first arbitrary probability bin.
                if ReturnEvent(lower=0) not in events:
                    raise ContractError("Probability conversion needs an explicit return > 0 event")
                positive = view.statement.probabilities[events.index(ReturnEvent(lower=0))] > 0.5
            else:
                # The strategy must give a meaningful zero to a raw score before using this rule.
                positive = view.statement.value > 0
            if positive:
                passing.add(instrument)
        # Passing assets share one account-NAV budget; leftover NAV remains unallocated cash.
        weight = self.budget / len(passing) if passing else 0
        # Explicit zero entries request exits for every configured asset that fails selection.
        positions = {i: weight if i in passing else 0 for i in self.instruments}
        metadata = _metadata(views, ctx, f"{ctx.node_id}:{ctx.snapshot_id}", self.lifetime)
        # Active weights may need maintenance trades as marks/NAV change; execution decides when.
        return NodeOutputs(
            {"positions": (PositionView(positions, "weight", self.scope, metadata),)}
        )
