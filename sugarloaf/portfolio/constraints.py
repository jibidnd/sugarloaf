"""Account-weight limits and the breach arithmetic shared by graph nodes and execution.

Everything here is a fraction of account NAV. For example, with NAV USD10,000 a
USD2,500 long position has weight 0.25 and a USD1,000 short has weight -0.10.
Gross exposure is the sum of absolute weights (0.35); net is the signed sum (0.15).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

# Shared neutral contracts permit the same checks with memory and native platform services.
from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, finite
from sugarloaf.core.views import PositionView

if TYPE_CHECKING:
    # Annotation only. core.services names PortfolioConstraints in a type hint, so
    # importing it here at runtime would create an import cycle.
    from sugarloaf.core.services import Context

# One comparison tolerance, in NAV fractions, shared by every limit below.
# Weights are floats computed from prices; an exact comparison would reject a
# target that sits on a limit only because of rounding.
TOLERANCE = 1e-8


@dataclass(frozen=True)
class PortfolioConstraints:
    """Exposure limits expressed as fractions of account NAV.

    Attributes:
        gross_limit: Maximum sum of absolute weights.
        net_min: Minimum signed sum of weights. Negative permits a net-short book.
        net_max: Maximum signed sum of weights. Lower it to hold a cash reserve:
            ``net_max=0.9`` keeps at least 10% of NAV uninvested.
        min_weight: Signed lower bound on each nonzero position. A negative value
            is what permits a short; a positive value is a minimum position size.
        max_weight: Signed upper bound on each nonzero position.
        turnover_limit: Maximum sum of absolute weight changes one target may
            request. It bounds a single decision, not turnover per unit of time:
            a graph that issues a new target every bar gets a fresh allowance each bar.

    Zero is always an allowed position, so an exit never violates ``min_weight``.

    The defaults describe an unlevered long-only account. To allow shorts, set a
    negative ``min_weight``; if the whole book may be net short, also set a
    negative ``net_min``. Each limit has exactly one field: there is no separate
    "allow short" switch or second gross/cash setting to keep consistent.
    """

    gross_limit: float = 1.0
    net_min: float = 0.0
    net_max: float = 1.0
    min_weight: float = 0.0
    max_weight: float = 1.0
    turnover_limit: float = 2.0

    def __post_init__(self):
        # NaN would make every comparison below False and silently disable a limit.
        finite(*vars(self).values())
        if (
            self.gross_limit < 0
            or self.turnover_limit < 0
            or self.min_weight > self.max_weight
            or self.net_min > self.net_max
        ):
            raise ContractError("Invalid portfolio constraints")


def position_weights(view: PositionView, ctx: "Context") -> dict[InstrumentId, float]:
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


def breaches(
    low: Mapping[InstrumentId, float],
    high: Mapping[InstrumentId, float],
    constraints: PortfolioConstraints,
) -> dict:
    """Measure how far a range of possible weights lies outside each limit.

    Args:
        low: Each instrument's lowest possible weight (every pending sell fills,
            no pending buy does).
        high: Each instrument's highest possible weight (every pending buy fills,
            no pending sell does). Pass the same mapping twice when the weights
            are definite, as a graph node checking a proposed target does.
        constraints: Limits to test.

    Returns:
        Violation size by limit, in NAV fractions. Zero means the limit holds.
        Account-wide limits are keyed by name, for example ``"gross exposure"``.
        Per-position limits are keyed by ``(name, instrument)``.

    Sizes, rather than pass/fail flags, let execution compare before and after:
    an order that shrinks an existing breach is allowed, one that grows it is not.
    For example, a 110% long position under a 100% gross limit breaches by 0.10;
    selling a tenth of it reduces the breach to zero and must not be blocked.
    """
    ids = set(low) | set(high)
    result = {
        # Buys and sells can fill in any order, so the worst gross exposure takes
        # each instrument's larger absolute side rather than netting them.
        "gross exposure": max(
            0.0,
            sum(max(abs(low.get(i, 0)), abs(high.get(i, 0))) for i in ids)
            - constraints.gross_limit,
        ),
        # The lowest possible net is tested against the floor, the highest against the cap.
        "minimum net exposure": max(0.0, constraints.net_min - sum(low.values())),
        "maximum net exposure": max(0.0, sum(high.values()) - constraints.net_max),
    }
    for instrument in ids:
        lowest, highest = low.get(instrument, 0), high.get(instrument, 0)
        # Zero is always permitted, so a bound applies only to a position that exists.
        # This is what lets a positive min_weight mean "at least 5% or nothing".
        if abs(lowest) > TOLERANCE:
            result["minimum position bound", instrument] = max(
                0.0, constraints.min_weight - lowest
            )
        if abs(highest) > TOLERANCE:
            result["maximum position bound", instrument] = max(
                0.0, highest - constraints.max_weight
            )
    return result


def breach_name(key) -> str:
    """Return a breach's limit name, dropping the instrument of a per-position key."""
    return key if isinstance(key, str) else key[0]


def violations(
    weights: Mapping[InstrumentId, float],
    constraints: PortfolioConstraints,
    current: Mapping[InstrumentId, float] | None = None,
) -> tuple[str, ...]:
    """Report failed portfolio limits without repairing the proposed weights.

    Args:
        weights: Effective proposed weights as account-NAV fractions.
        constraints: Gross, net, position, and turnover limits.
        current: Optional current weights for the absolute-change turnover check.

    Returns:
        Sorted names of every failed limit, or an empty tuple when feasible.

    Callers must merge held but unspecified instruments when omission means hold.
    These checks report; they never clip, rescale or otherwise repair a target.
    """
    # Definite weights are simply a range whose low and high sides coincide, so
    # nodes and the execution risk gate share one implementation of every limit.
    failures = {
        breach_name(key)
        for key, excess in breaches(weights, weights, constraints).items()
        if excess > TOLERANCE
    }
    if current is not None:
        # Both entering and exiting assets contribute their absolute account-weight change.
        # The caller must already retain unspecified holdings in the effective target.
        turnover = sum(
            abs(weights.get(i, 0) - current.get(i, 0)) for i in set(weights) | set(current)
        )
        if turnover > constraints.turnover_limit + TOLERANCE:
            failures.add("turnover")
    return tuple(sorted(failures))
