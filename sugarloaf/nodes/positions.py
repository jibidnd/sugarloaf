"""Explicit portfolio branch combination and account constraint validation."""

from datetime import timedelta
from math import isfinite
from typing import Mapping

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import InputSpec, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.execution.contracts import Portfolio
from sugarloaf.portfolio.constraints import PortfolioConstraints, violations


def position_weights(view: PositionView, portfolio: Portfolio) -> dict[InstrumentId, float]:
    """A target as account-NAV fractions at current marks, keeping explicit zeros.

    Raises:
        ContractError: Account NAV is not positive.
        NotReady: A quantity position has no mark.
    """
    if portfolio.nav <= 0:
        raise ContractError("Positive account NAV is required")
    if view.units == "weight":
        return dict(view.positions)
    weights = {}
    for instrument, quantity in view.positions.items():
        price = portfolio.prices.get(instrument)
        if quantity and (price is None or price <= 0):
            raise NotReady(f"No mark for {instrument.value}")
        weights[instrument] = quantity * (price or 0.0) / portfolio.nav
    return weights


class PositionNetter:
    """Combine named branch targets into account weights with preserved attribution.

    Args:
        budgets: Gross account-NAV limits by required input port/branch scope.
        lifetime: Maximum output lifetime, also bounded by parent expiry.

    Each branch supplies exactly one PositionView. Previously nonzero instruments
    require explicit zero exits before omission is allowed.
    """

    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def __init__(self, budgets: Mapping[str, float], lifetime: timedelta = timedelta(days=4)):
        if not budgets or any(b < 0 or not isfinite(b) for b in budgets.values()):
            raise ContractError("Netter requires finite nonnegative branch budgets")
        self.budgets = dict(budgets)
        self.ports = {port: InputSpec((PositionView,), 1, 1) for port in budgets}
        self.lifetime = lifetime
        self.config = {"budgets": dict(budgets)}
        self._nonzero: dict[str, set] = {}

    def update(self, ctx, data, inputs):
        """Convert branch quantities to weights, enforce budgets, and sum contributions.

        Raises:
            NotReady: A required branch is missing.
            ContractError: Scope mismatches, a managed position disappears, or a branch
                exceeds its gross account-weight budget.
        """
        combined: dict = {}
        attribution = {}
        parents = []
        next_nonzero = {}
        expiry = ctx.time + self.lifetime
        portfolio = None
        for port, budget in self.budgets.items():
            views = inputs.get(port, ())
            if len(views) != 1:
                raise NotReady(f"Missing position branch {port}")
            view = views[0]
            if view.scope != port:
                raise ContractError(f"Branch {port} supplied scope {view.scope}")
            if view.units == "quantity" and portfolio is None:
                portfolio = ctx.portfolio
            weights = position_weights(view, portfolio) if view.units == "quantity" else dict(
                view.positions
            )
            if self._nonzero.get(port, set()) - set(weights):
                raise ContractError(f"Branch {port} omitted a managed position; emit explicit zero")
            next_nonzero[port] = {i for i, weight in weights.items() if weight != 0}
            if sum(abs(w) for w in weights.values()) > budget + 1e-8:
                raise ContractError(f"Branch {port} exceeds budget")
            attribution[port] = weights
            for instrument, weight in weights.items():
                combined[instrument] = combined.get(instrument, 0.0) + weight
            parents.append(view.metadata.view_id)
            expiry = min(expiry, view.metadata.expires_at)
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, expiry, tuple(parents)
        )
        self._nonzero = next_nonzero
        return {"positions": (PositionView(combined, "weight", "account", metadata, attribution),)}


class PortfolioConstraintNode:
    """Reject infeasible account targets; no implicit rescaling.

    The runner checks the terminal target against the deployment's constraints as
    well; this node lets a graph apply stricter limits to a branch, or report a
    rejection as a withdrawn view before anything reaches execution.
    """

    ports = {"positions": InputSpec((PositionView,), 1, 1)}
    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def __init__(self, constraints: PortfolioConstraints):
        self.constraints = constraints
        self.config = {"constraints": constraints}

    def update(self, ctx, data, inputs):
        """Validate proposed weights over held positions and preserve parent attribution.

        Raises:
            NotReady: Effective account positions violate configured limits.
        """
        view = inputs["positions"][0]
        portfolio = ctx.portfolio
        weights = position_weights(view, portfolio)
        current = portfolio.weights()
        # Unspecified instruments remain held. Omission is not an exit instruction.
        effective = {**current, **weights}
        failures = violations(effective, self.constraints, current)
        if failures:
            raise NotReady("TARGET_REJECTED: " + ", ".join(failures))
        if view.units == "weight":
            attribution = view.attribution
        else:
            attribution = {
                branch: {
                    i: q * portfolio.prices.get(i, 0.0) / portfolio.nav
                    for i, q in contributions.items()
                }
                for branch, contributions in view.attribution.items()
            }
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.sequence}",
            ctx.node_id,
            ctx.time,
            view.metadata.expires_at,
            (view.metadata.view_id,),
        )
        return {"positions": (PositionView(weights, "weight", "account", metadata, attribution),)}
