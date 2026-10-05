"""Shared feasibility checks in fractions of account NAV."""

from typing import Mapping

# Shared neutral contracts permit the same checks with memory and native platform services.
from sugarloaf.core.errors import ContractError
from sugarloaf.core.services import Context, PortfolioConstraints
from sugarloaf.core.types import InstrumentId
from sugarloaf.core.views import PositionView


def position_weights(view: PositionView, ctx: Context) -> dict[InstrumentId, float]:
    """Convert a target to account-NAV fractions using current marks and FX.

    Args:
        view: Weight or quantity target; quantities include instrument multipliers.
        ctx: Fresh portfolio and valuation services.

    Returns:
        Instrument weights, preserving explicit zeros.

    Raises:
        ContractError: Account NAV is nonpositive.
    """
    account = ctx.portfolio.snapshot()
    # A positive denominator is essential; a zero/negative NAV cannot define account weights.
    if account.nav <= 0:
        raise ContractError("Positive account NAV is required")
    if view.units == "weight":
        # Copy the mapping while retaining explicit zero exits and signed short weights.
        return dict(view.positions)
    # unit_value includes the mark, share/contract multiplier and account-currency FX.
    # Quantity times unit value is exposure, separate from a broker's margin requirement.
    return {i: q * ctx.valuation.unit_value(i) / account.nav for i, q in view.positions.items()}


def violations(
    weights: Mapping[InstrumentId, float],
    constraints: PortfolioConstraints,
    current: Mapping[InstrumentId, float] | None = None,
) -> tuple[str, ...]:
    """Report failed portfolio limits without repairing the proposed weights.

    Args:
        weights: Effective proposed weights as account-NAV fractions.
        constraints: Gross, net, position, cash-reserve, and turnover limits.
        current: Optional current weights for absolute-change turnover checks.

    Returns:
        Failed bound descriptions, or an empty tuple when feasible.

    Callers must merge held but unspecified instruments when omission means hold.
    Zero is always an allowed exit; comparison tolerance is 1e-8.
    """
    tolerance = 1e-8
    # Return every failed bound for diagnostics; these checks do not repair a target.
    failures = []
    values = tuple(weights.values())
    gross, net = sum(abs(w) for w in values), sum(values)
    # Gross counts both signs; net permits offsets but does not remove shorting restrictions.
    if any(w < -tolerance for w in values) and not constraints.allow_short:
        failures.append("shorting disabled")
    # Zero is always a permitted exit, even if a positive minimum is configured.
    if any(
        abs(w) > tolerance
        and (w < constraints.min_weight - tolerance or w > constraints.max_weight + tolerance)
        for w in values
    ):
        failures.append("position bounds")
    if gross > min(constraints.gross_limit, constraints.budget) + tolerance:
        failures.append("gross exposure/budget")
    if net < constraints.net_min - tolerance or net > constraints.net_max + tolerance:
        failures.append("net exposure")
    if net > 1 - constraints.cash_reserve + tolerance:
        # This is a NAV-fraction bound, not a promise of broker cash after pending fills/costs.
        failures.append("cash reserve")
    if current is not None:
        # Both entering and exiting assets contribute their absolute account-weight change.
        # The caller must already retain unspecified holdings in the effective target.
        turnover = sum(
            abs(weights.get(i, 0) - current.get(i, 0)) for i in set(weights) | set(current)
        )
        if turnover > constraints.turnover_limit + tolerance:
            failures.append("turnover")
    return tuple(failures)
