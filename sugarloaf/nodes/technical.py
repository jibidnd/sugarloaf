"""Small native-indicator score nodes for research and long/cash baselines.

Indicators come from the adapter. Scores express a direction, not an expected
return; connect one to PositiveSignalPositions for a naive trading graph.
"""

# Durations define bar cadence and elapsed-calendar forecast horizons.
from datetime import timedelta
from math import isfinite

# Nodes depend on neutral contracts; adapters supply actual native indicator objects.
from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.services import Context
from sugarloaf.core.views import Score, ScoreView, return_target
from sugarloaf.market.contracts import (
    BarsRequirement,
    IndicatorRequirement,
    InstrumentRequirement,
    Selection,
    Trigger,
)

# A forecast horizon is either elapsed calendar time or a count of trading sessions.
Horizon = timedelta | int


def check_horizon(horizon: Horizon) -> None:
    """Reject a horizon that is neither a positive duration nor a positive session count."""
    # bool is a subclass of int in Python; reject it so True is not read as "1 session".
    if isinstance(horizon, bool) or not isinstance(horizon, (timedelta, int)):
        raise ContractError("Horizon must be a timedelta or a whole number of sessions")
    if horizon <= (timedelta(0) if isinstance(horizon, timedelta) else 0):
        raise ContractError("Positive forecast horizon required")


def horizon_end(ctx: Context, instrument: InstrumentId, horizon: Horizon):
    """Return when a forecast issued now, over this horizon, ends.

    Args:
        ctx: Evaluation Context supplying graph time and the exchange calendar.
        instrument: Instrument whose exchange sessions are counted.
        horizon: A ``timedelta`` is elapsed calendar time. An ``int`` counts
            regular trading sessions and ends at the close of the last one.

    The two differ around weekends and holidays. From Friday's 16:00 close:

    * ``timedelta(days=1)`` ends Saturday 16:00. No price exists there to score
      the forecast, and a position target with that expiry lapses before the
      market reopens, so the Friday signal can never trade.
    * ``1`` (one session) ends Monday 16:00, the next daily close.

    Issued during a session, a horizon of one session ends at that same day's close.
    A node using a session horizon must declare an InstrumentRequirement for the
    instrument, because the calendar is read through ``ctx.instruments``.
    """
    if isinstance(horizon, timedelta):
        return ctx.now + horizon
    return ctx.instruments.session_close(instrument, ctx.now, horizon)


def _period(value):
    # Reject booleans despite their integer subtype: resource settings must be explicit periods.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("Indicator periods must be positive integers")
    return value


class _IndicatorScore:
    # Indicator source nodes have no upstream messages and publish one scalar score family.
    inputs = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instrument, interval, horizon):
        if interval <= timedelta(0):
            raise ContractError("Positive bar interval required")
        check_horizon(horizon)
        self.instrument, self.horizon = instrument, horizon
        # One declared input convention backs every indicator this node requests.
        self.source = BarsRequirement(Selection((instrument,)), interval, normalization="adjusted")
        self.triggers = (Trigger("bar_close", instrument, interval),)
        # Counting sessions reads the exchange calendar, which needs its own
        # declaration. Subclasses append this to their requirements; it is empty
        # for a calendar-time horizon, which needs no calendar at all.
        self._calendar = (
            (InstrumentRequirement(Selection((instrument,))),) if isinstance(horizon, int) else ()
        )

    def _point(self, requirement, ctx):
        # Snapshot reads enforce declared ownership/readiness; never retain Context across batches.
        point = ctx.market.indicator(requirement, self.instrument).points[-1]
        if point.time > ctx.now or not isfinite(point.values["value"]):
            # Future timestamps and invalid numeric values cannot become decision evidence.
            raise NotReady("Indicator needs a finite value available at the current data clock")
        return point

    def _output(self, value, ctx):
        # The target interval and this View's authority end together, at the horizon.
        end = horizon_end(ctx, self.instrument, self.horizon)
        view = ScoreView(
            return_target(self.instrument, Interval(ctx.now, end)),
            Score(value, self.calibration_id),
            # Calibration identity distinguishes score definitions; it is not a fitted model.
            Metadata(f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, end),
        )
        return NodeOutputs({"views": (view,)})


class EMASpread(_IndicatorScore):
    """Fast EMA divided by slow EMA minus one, emitted as an uncalibrated score.

    Args:
        instrument (InstrumentId): Stable identity for the provider-native averages.
        fast (int): Fast EMA period in bars; positive and below slow.
        slow (int): Slow EMA period in bars.
        interval (timedelta): Completed bar duration for both averages.
        horizon (timedelta | int): Length of the score target and its expiry: elapsed
            calendar time, or a count of trading sessions (see ``horizon_end``).
    """

    def __init__(self, instrument, fast=20, slow=50, *, interval=timedelta(days=1), horizon=1):
        if _period(fast) >= _period(slow):
            raise ContractError("EMA fast period must be below slow period")
        super().__init__(instrument, interval, horizon)
        # Periods are measured in declared source bars; the provider owns seeding and updates.
        self.fast = IndicatorRequirement(self.source, "ema", (("period", fast),))
        self.slow = IndicatorRequirement(self.source, "ema", (("period", slow),))
        self.requirements = (self.fast, self.slow, *self._calendar)
        # Include rule settings in identity so aggregation cannot mix different score meanings.
        self.calibration_id = f"ema-spread:{fast}:{slow}:{interval.total_seconds()}:v1"

    def evaluate(self, inputs, ctx):
        """Emit the current spread; require aligned native points and a positive slow EMA."""
        # Two individually ready resources still need the same observation boundary.
        fast, slow = self._point(self.fast, ctx), self._point(self.slow, ctx)
        if fast.time != slow.time or slow.values["value"] <= 0:
            raise NotReady("EMA spread needs aligned points and a positive denominator")
        # Ratio-minus-one is scale-free direction, not a calibrated expected return.
        return self._output(fast.values["value"] / slow.values["value"] - 1, ctx)


class RSIReversion(_IndicatorScore):
    """Threshold minus native RSI, divided by 100; positive below the threshold.

    Args:
        instrument (InstrumentId): Stable instrument identity.
        period (int): Positive RSI period in bars.
        threshold (float): RSI level in [0, 100]; no holding hysteresis is applied.
        interval (timedelta): Completed bar duration.
        horizon (timedelta | int): Length of the score target and its expiry: elapsed
            calendar time, or a count of trading sessions (see ``horizon_end``).
    """

    def __init__(
        self,
        instrument,
        period=14,
        threshold=30,
        *,
        interval=timedelta(days=1),
        horizon=1,
    ):
        _period(period)
        if not isfinite(threshold) or not 0 <= threshold <= 100:
            raise ContractError("RSI threshold must lie in [0, 100]")
        super().__init__(instrument, interval, horizon)
        self.threshold = threshold
        # No alternate local RSI formula is substituted when native readiness is unavailable.
        self.rsi = IndicatorRequirement(self.source, "rsi", (("period", period),))
        self.requirements = (self.rsi, *self._calendar)
        self.calibration_id = f"rsi-reversion:{period}:{threshold}:{interval.total_seconds()}:v1"

    def evaluate(self, inputs, ctx):
        """Emit a reversion score from a finite native RSI in [0, 100]."""
        value = self._point(self.rsi, ctx).values["value"]
        if not 0 <= value <= 100:
            raise NotReady("RSI observation must lie in [0, 100]")
        # Division by 100 normalizes direction. Equality is neutral; no hold/exit hysteresis exists.
        return self._output((self.threshold - value) / 100, ctx)


class SMATrend(_IndicatorScore):
    """Completed close divided by native SMA minus one; positive above the average.

    Args:
        instrument (InstrumentId): Stable instrument identity.
        period (int): Positive SMA period in bars.
        interval (timedelta): Completed bar duration for price and average.
        horizon (timedelta | int): Length of the score target and its expiry: elapsed
            calendar time, or a count of trading sessions (see ``horizon_end``).
    """

    def __init__(self, instrument, period=210, *, interval=timedelta(days=1), horizon=1):
        _period(period)
        super().__init__(instrument, interval, horizon)
        self.average = IndicatorRequirement(self.source, "sma", (("period", period),))
        # This rule reads adjusted bars as well as their native average, so declare both.
        self.requirements = (self.source, self.average, *self._calendar)
        self.calibration_id = f"sma-trend:{period}:{interval.total_seconds()}:v1"

    def evaluate(self, inputs, ctx):
        """Emit a trend score; require the completed close and SMA to share a timestamp."""
        # Use the latest admitted completed close, rather than a live mark or partial bar.
        bar = ctx.market.bars(self.source, self.instrument)[-1]
        average = self._point(self.average, ctx)
        # Bars may carry non-positive prices, so the denominator is checked here.
        if bar.end != average.time or average.values["value"] <= 0:
            raise NotReady("Trend needs aligned completed bars and a positive SMA")
        # Price above its average is positive; allocation/scheduling belongs to downstream nodes.
        return self._output(bar.close / average.values["value"] - 1, ctx)
