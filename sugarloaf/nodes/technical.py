"""Small native-indicator score nodes.

Each node declares an ``Indicator`` the platform computes and warms, reads it on
every bar of its instrument, and emits a ``ScoreView``: a direction, not an
expected return. Connect one to ``PositiveSignalPositions`` for a naive trading
graph.
"""

from datetime import timedelta
from math import isfinite

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.inputs import Bars, Indicator, Selection
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import Score, ScoreView, return_target

# A forecast horizon is either elapsed calendar time or a count of trading sessions.
Horizon = timedelta | int


def check_horizon(horizon: Horizon) -> None:
    """Reject a horizon that is neither a positive duration nor a positive session count."""
    if isinstance(horizon, bool) or not isinstance(horizon, (timedelta, int)):
        raise ContractError("Horizon must be a timedelta or a whole number of sessions")
    if horizon <= (timedelta(0) if isinstance(horizon, timedelta) else 0):
        raise ContractError("Positive forecast horizon required")


def horizon_end(ctx, instrument: InstrumentId, horizon: Horizon):
    """When a forecast issued now, over this horizon, ends.

    A ``timedelta`` is elapsed calendar time. An ``int`` counts regular trading
    sessions and ends at the close of the last one, read from the platform's
    exchange calendar: from Friday's close, one session ends at Monday's close,
    where a price exists to score the forecast and the market is open to trade it.
    """
    if isinstance(horizon, timedelta):
        return ctx.time + horizon
    return ctx.calendar(instrument).next_close(ctx.time, horizon)


def _period(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("Indicator periods must be positive integers")
    return value


class _IndicatorScore:
    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instrument, resolution, interval, horizon):
        check_horizon(horizon)
        self.instrument, self.horizon = instrument, horizon
        self.resolution, self.interval = resolution, interval
        self.selection = Selection((instrument,))
        # The bar feed drives the callbacks; the indicators consume the same feed.
        self.bars = Bars(self.selection, resolution, interval, normalization="adjusted")
        self.inputs = (self.bars,)
        # The latest output, restated when a slice carries no bar of the instrument
        # (a dividend, say) so that a consumer keeps its evidence until the next bar.
        self._current = None

    def _indicator(self, name, kind, period, **parameters):
        return Indicator(
            name, kind, period, self.selection, self.resolution, self.interval,
            parameters=tuple(parameters.items()),
        )

    def _reading(self, ctx, name):
        reading = ctx.indicator(name, self.instrument)
        if not reading.is_ready or reading.value is None or not isfinite(reading.value):
            raise NotReady(f"{name} is not ready")
        return reading

    def _score(self, ctx, value):
        end = horizon_end(ctx, self.instrument, self.horizon)
        view = ScoreView(
            return_target(self.instrument, Interval(ctx.time, end)),
            Score(value, self.calibration_id),
            Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, end),
        )
        return {"views": (view,)}

    def update(self, ctx, data, views):
        if self.instrument not in data.bars:
            return self._current
        self._current = self._evaluate(ctx, data.bars[self.instrument])
        return self._current

    def _evaluate(self, ctx, bar):
        raise NotImplementedError


class EMASpread(_IndicatorScore):
    """Fast EMA divided by slow EMA minus one, as an uncalibrated score.

    Args:
        instrument: The instrument.
        fast: Fast EMA period in bars; positive and below slow.
        slow: Slow EMA period in bars.
        resolution: Bar feed: minute, hour or daily.
        interval: Consolidation of that feed, or None for its own bars.
        horizon: Length of the score target and its expiry: elapsed calendar time,
            or a count of trading sessions (see ``horizon_end``).
    """

    def __init__(self, instrument, fast=20, slow=50, *, resolution="daily", interval=None,
                 horizon=1):
        if _period(fast) >= _period(slow):
            raise ContractError("EMA fast period must be below slow period")
        super().__init__(instrument, resolution, interval, horizon)
        self.indicators = (
            self._indicator("fast", "ema", fast),
            self._indicator("slow", "ema", slow),
        )
        self.config = {"fast": fast, "slow": slow, "resolution": resolution,
                       "interval": interval, "horizon": horizon}
        self.calibration_id = f"ema-spread:{fast}:{slow}:{self.bars.period.total_seconds()}:v1"

    def _evaluate(self, ctx, bar):
        fast, slow = self._reading(ctx, "fast"), self._reading(ctx, "slow")
        if fast.time != slow.time or slow.value <= 0:
            raise NotReady("EMA spread needs aligned readings and a positive denominator")
        return self._score(ctx, fast.value / slow.value - 1)


class RSIReversion(_IndicatorScore):
    """Threshold minus native RSI, divided by 100; positive below the threshold."""

    def __init__(self, instrument, period=14, threshold=30, *, resolution="daily",
                 interval=None, horizon=1):
        _period(period)
        if not isfinite(threshold) or not 0 <= threshold <= 100:
            raise ContractError("RSI threshold must lie in [0, 100]")
        super().__init__(instrument, resolution, interval, horizon)
        self.threshold = threshold
        self.indicators = (self._indicator("rsi", "rsi", period),)
        self.config = {"period": period, "threshold": threshold, "resolution": resolution,
                       "interval": interval, "horizon": horizon}
        self.calibration_id = (
            f"rsi-reversion:{period}:{threshold}:{self.bars.period.total_seconds()}:v1"
        )

    def _evaluate(self, ctx, bar):
        value = self._reading(ctx, "rsi").value
        if not 0 <= value <= 100:
            raise NotReady("RSI observation must lie in [0, 100]")
        return self._score(ctx, (self.threshold - value) / 100)


class SMATrend(_IndicatorScore):
    """Completed close divided by native SMA minus one; positive above the average."""

    def __init__(self, instrument, period=210, *, resolution="daily", interval=None, horizon=1):
        _period(period)
        super().__init__(instrument, resolution, interval, horizon)
        self.indicators = (self._indicator("sma", "sma", period),)
        self.config = {"period": period, "resolution": resolution, "interval": interval,
                       "horizon": horizon}
        self.calibration_id = f"sma-trend:{period}:{self.bars.period.total_seconds()}:v1"

    def _evaluate(self, ctx, bar):
        average = self._reading(ctx, "sma")
        if bar.end != average.time or average.value <= 0:
            raise NotReady("Trend needs the completed close and SMA to align")
        return self._score(ctx, bar.close / average.value - 1)
