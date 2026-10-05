"""Small batch reference calculations, not a streaming indicator service.

Every call seeds from the supplied history. A rolling history window therefore
differs from an indicator maintained continuously since deployment start.
"""

# Accept ordinary ordered price histories; calculations operate on numeric arrays.
from collections.abc import Sequence

# NumPy handles history validation; SciPy evaluates the explicit smoothing recurrence.
import numpy as np
from scipy.signal import lfilter

from sugarloaf.core.errors import ContractError, NotReady


def _prices(values: Sequence[float], period: int, extra: int = 0) -> np.ndarray:
    # bool is an int subclass, but True is not a meaningful period setting.
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ContractError("Indicator period must be a positive integer")
    prices = np.asarray(values, dtype=float)
    # Reject malformed marks instead of silently dropping observations or filling gaps.
    if prices.ndim != 1 or not np.all(np.isfinite(prices)) or np.any(prices <= 0):
        raise ContractError("Expected a one-dimensional sequence of positive finite prices")
    if len(prices) < period + extra:
        # Insufficient observations are readiness, rather than an invalid strategy rule.
        raise NotReady("Insufficient indicator history")
    return prices


def _smooth(values: np.ndarray, period: int, alpha: float) -> float:
    """SMA seed followed by y[t] = alpha*x[t] + (1-alpha)*y[t-1]."""
    # Every supplied history has its own seed; sliding that history changes the result.
    seed = float(np.mean(values[:period]))
    if len(values) == period:
        return seed
    # The filter denominator carries the previous value into the next recurrence step.
    # Its initial state gives the first post-seed observation the correct seed contribution.
    result, _ = lfilter([alpha], [1, -(1 - alpha)], values[period:], zi=[(1 - alpha) * seed])
    return float(result[-1])


def ema(values: Sequence[float], period: int) -> float:
    """Latest EMA, seeded by the first period's SMA; alpha = 2/(period+1)."""
    # This batch reference is explicit teaching code, not a native-provider fallback.
    return _smooth(_prices(values, period), period, 2 / (period + 1))


def rsi(values: Sequence[float], period: int = 14) -> float:
    """Wilder RSI: SMA-seeded gains/losses, then alpha = 1/period.

    Flat history returns 50, gains without losses 100, losses without gains 0.
    These are explicit example conventions; provider edge cases can differ.
    """
    # N changes require N+1 prices. Positive and negative changes become separate magnitudes.
    changes = np.diff(_prices(values, period, extra=1))
    gain = _smooth(np.maximum(changes, 0), period, 1 / period)
    loss = _smooth(np.maximum(-changes, 0), period, 1 / period)
    # Resolve the otherwise undefined zero-gain/zero-loss ratio with this fixture convention.
    if gain == loss == 0:
        return 50.0
    # This equivalent RSI ratio naturally produces 100 for all-up and 0 for all-down data.
    return 100 * gain / (gain + loss)
