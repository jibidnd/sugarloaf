"""Point-in-time universe selection over rows the adapter supplies.

The rule is neutral; the adapter owns the platform's refresh callback and converts
its rows. Selection never fetches data and never guesses a missing field.
"""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from sugarloaf.core.errors import CapabilityError, ContractError, NotReady
from sugarloaf.core.inputs import (
    ETFConstituentsUniverse,
    FundamentalUniverse,
    LiquidityUniverse,
    Universe,
)
from sugarloaf.core.primitives import InstrumentId


@dataclass(frozen=True)
class SelectionRow:
    """Available selection inputs for one instrument."""

    instrument: InstrumentId
    price: float | None = None
    volume: float | None = None
    dollar_volume: float | None = None
    weight: float | None = None
    # Whether the platform has company fundamentals for the security; None when
    # the platform cannot say.
    has_fundamentals: bool | None = None


def _first_distinct(rows, count):
    """The first ``count`` distinct identities of ranked rows.

    A platform can list one security twice in a day's selection data. A member is
    selected once, by its best-ranked row, and a repeat does not use up a place.
    """
    members = dict.fromkeys(row.instrument for row in rows)
    return tuple(members)[:count]


def select(definition: Universe, rows, at: datetime | None = None) -> tuple[InstrumentId, ...]:
    """Select members for one refresh.

    Args:
        definition: Liquidity, ETF constituent or fundamental rule.
        rows: ``SelectionRow`` values, or ``FundamentalRecord`` values for a
            ``FundamentalUniverse``.
        at: The selection time, required by a ``FundamentalUniverse``.

    Returns:
        Ordered selected identities; ties break on the identity so the result does
        not depend on the platform's iteration order.

    Raises:
        NotReady: Required selection inputs are missing.
        CapabilityError: The universe definition is unsupported.
    """
    if isinstance(definition, LiquidityUniverse):
        if any(r.price is None or r.volume is None or r.dollar_volume is None for r in rows):
            raise NotReady("Liquidity selection fields missing")
        if definition.require_fundamentals and any(r.has_fundamentals is None for r in rows):
            # An unknown is not a no: selecting on a guess would change membership.
            raise NotReady("Liquidity selection needs to know which securities have fundamentals")
        rows = [
            r
            for r in rows
            if all(isfinite(v) for v in (r.price, r.volume, r.dollar_volume))
            and r.price >= definition.min_price
            and r.volume >= definition.min_volume
            and (r.has_fundamentals or not definition.require_fundamentals)
        ]
        rows.sort(key=lambda r: (-r.dollar_volume, r.instrument.value))
        return _first_distinct(rows, definition.count)
    if isinstance(definition, ETFConstituentsUniverse):
        if definition.missing_weight == "error" and any(r.weight is None for r in rows):
            raise NotReady("ETF constituent weight missing")
        rows = [
            r
            for r in rows
            if (r.weight is not None and isfinite(r.weight))
            or definition.missing_weight == "include"
        ]
        if definition.min_weight is not None:
            rows = [r for r in rows if r.weight is not None and r.weight >= definition.min_weight]
        rows.sort(key=lambda r: (-(r.weight if r.weight is not None else -1), r.instrument.value))
        return _first_distinct(rows, definition.count)
    if isinstance(definition, FundamentalUniverse):
        if at is None:
            raise ContractError("Fundamental selection needs its selection time")
        rows = tuple(rows)
        available = {r.instrument for r in rows}
        picked = list(definition.rule(at, rows))
        if any(not isinstance(i, InstrumentId) for i in picked):
            raise ContractError("A fundamental rule must return InstrumentId values")
        # The rule chooses; selection keeps only securities the platform listed,
        # once each, in the rule's order.
        return tuple(dict.fromkeys(i for i in picked if i in available))
    raise CapabilityError("Unsupported universe definition")
