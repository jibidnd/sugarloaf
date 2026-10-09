"""Explicit portfolio branch combination and account constraint validation."""

from datetime import timedelta
from math import isfinite
from typing import Mapping

# Shared arithmetic keeps quantity valuation and feasibility consistent between position nodes.
from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import InputSpec, NodeInputs, NodeOutputs, OutputSpec
from sugarloaf.core.primitives import Metadata
from sugarloaf.core.services import Context
from sugarloaf.core.views import PositionView
from sugarloaf.market.contracts import InstrumentRequirement, PortfolioRequirement, Selection
from sugarloaf.portfolio.constraints import PortfolioConstraints, position_weights, violations


class PositionNetter:
    """Combine named branch targets into account weights with preserved attribution.

    Args:
        budgets: Gross account-NAV limits by required input port/branch scope.
            Limits validate contributions; they do not multiply branch weights.
        lifetime: Maximum output lifetime, also bounded by parent expiry.

    Each branch supplies exactly one PositionView. Previously nonzero instruments
    require explicit zero exits before omission is allowed.
    """

    outputs = {"positions": OutputSpec((PositionView,))}
    requirements = (PortfolioRequirement(), InstrumentRequirement(Selection()))
    # Account/metadata access supports quantity conversion without declaring price subscriptions.
    triggers = ()

    # Four days is a backstop that spans a long weekend; each branch target's own
    # expiry is the binding limit. One calendar day would end a Friday-close
    # account target on Saturday even when every branch is valid until Monday.
    def __init__(self, budgets: Mapping[str, float], lifetime: timedelta = timedelta(days=4)):
        if not budgets or any(b < 0 or not isfinite(b) for b in budgets.values()):
            raise ContractError("Netter requires finite nonnegative branch budgets")
        self.budgets = dict(budgets)
        # Port names also identify branch scopes; each branch must send one complete target.
        self.inputs = {port: InputSpec((PositionView,), 1, 1) for port in budgets}
        self.lifetime = lifetime
        self._nonzero: dict[str, set] = {}

    def evaluate(self, inputs: NodeInputs, ctx: Context) -> NodeOutputs:
        """Convert branch quantities to weights, enforce budgets, and sum contributions.

        Raises:
            NotReady: A required branch is missing.
            ContractError: Scope mismatches, a managed position disappears, or a branch
                exceeds its gross account-weight budget.
        """
        combined = {}
        attribution = {}
        parents = []
        next_nonzero = {}
        # Stage branch-management state until every branch has passed validation.
        expiry = ctx.now + self.lifetime
        for port, budget in self.budgets.items():
            views = inputs.by_port.get(port, ())
            if len(views) != 1:
                raise NotReady(f"Missing position branch {port}")
            view = views[0]
            if view.scope != port:
                raise ContractError(f"Branch {port} supplied scope {view.scope}")
            # Quantities convert with current account marks/multipliers/FX; weights already use NAV.
            weights = position_weights(view, ctx)
            # A disappeared key cannot safely communicate an exit from a previously managed asset.
            if self._nonzero.get(port, set()) - set(weights):
                raise ContractError(f"Branch {port} omitted a managed position; emit explicit zero")
            next_nonzero[port] = {i for i, weight in weights.items() if weight != 0}
            # Gross budget checks absolute contributions before any offsets across branches.
            if sum(abs(w) for w in weights.values()) > budget + 1e-8:
                raise ContractError(f"Branch {port} exceeds budget")
            attribution[port] = weights
            # Attribution records desired contributions, not ownership of actual filled shares.
            for instrument, weight in weights.items():
                combined[instrument] = combined.get(instrument, 0.0) + weight
            parents.append(view.metadata.view_id)
            # The net target cannot retain authority after any required branch expires.
            expiry = min(expiry, view.metadata.expires_at)
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, expiry, tuple(parents)
        )
        self._nonzero = next_nonzero
        return NodeOutputs(
            {"positions": (PositionView(combined, "weight", "account", metadata, attribution),)}
        )


class PortfolioConstraintNode:
    """Reject infeasible account targets; no implicit rescaling."""

    inputs = {"positions": InputSpec((PositionView,), 1, 1)}
    outputs = {"positions": OutputSpec((PositionView,))}
    requirements = (PortfolioRequirement(), InstrumentRequirement(Selection()))
    triggers = ()

    def __init__(self, constraints: PortfolioConstraints):
        self.constraints = constraints

    def evaluate(self, inputs: NodeInputs, ctx: Context) -> NodeOutputs:
        """Validate proposed weights over held positions and preserve parent attribution.

        Unspecified holdings remain held. Rejected targets are not rescaled.

        Raises:
            NotReady: Effective account positions violate configured limits.
        """
        view = inputs.by_port["positions"][0]
        account = ctx.portfolio.snapshot()
        # The proposed target and retained current holdings must share account-NAV units.
        weights = position_weights(view, ctx)
        current = {
            i: q * ctx.valuation.unit_value(i) / account.nav
            for i, q in account.holdings.items()
            if q != 0
        }
        # Unspecified instruments remain held. Omission is not an exit instruction.
        effective = {**current, **weights}
        failures = violations(effective, self.constraints, current)
        if failures:
            # Readiness failure withdraws authority; there is no clipping, rescaling or liquidation.
            raise NotReady("TARGET_REJECTED: " + ", ".join(failures))
        metadata = Metadata(
            # Fresh identity preserves the proposed target's parent, expiry and branch attribution.
            f"{ctx.node_id}:{ctx.snapshot_id}",
            ctx.node_id,
            ctx.now,
            view.metadata.expires_at,
            (view.metadata.view_id,),
        )
        return NodeOutputs(
            {"positions": (PositionView(weights, "weight", "account", metadata, view.attribution),)}
        )
