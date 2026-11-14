"""One native-indicator node, usable alone in a forecast run or in a trading graph.

See examples/EMA_RESEARCH.md for the idea, its assumptions and the evidence.
"""

from dataclasses import dataclass
from datetime import timedelta

from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import Bars, Indicator, Selection
from sugarloaf.core.node import InputSpec, OutputSpec
from sugarloaf.core.primitives import Interval, Metadata
from sugarloaf.core.views import PositionView, Score, ScoreView, return_target
from sugarloaf.nodes.positions import PortfolioConstraintNode
from sugarloaf.nodes.technical import Horizon, check_horizon, horizon_end
from sugarloaf.portfolio.constraints import PortfolioConstraints


@dataclass(frozen=True)
class EMAConfig:
    # Periods count bars of the declared cadence: with a five-minute interval,
    # 20/50 five-minute bars.
    fast: int = 20
    slow: int = 50
    resolution: str = "daily"
    interval: timedelta | None = None
    # How far ahead the score looks, and how long its view stays valid. An int
    # counts trading sessions: 1 means "until the next regular close", so a
    # Friday-close forecast ends at Monday's close, where a price exists to score
    # it and the market is open to trade it.
    horizon: Horizon = 1
    weight: float = 0.2

    def __post_init__(self):
        if not 0 < self.fast < self.slow or not 0 < self.weight <= 1:
            raise ContractError("Require 0 < fast < slow and weight in (0, 1]")
        check_horizon(self.horizon)


class EMAScore:
    """Fast/slow EMA spread as a score, not a calibrated return estimate.

    The node declares one bar feed and two native EMAs on it. The platform creates,
    warms and updates the EMAs; on each completed bar of the feed the node reads
    them and emits one ``ScoreView``.
    """

    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instrument, config=EMAConfig()):
        self.instrument, self.config = instrument, config
        selection = Selection((instrument,))
        # Raw prices: the spread is read off the traded series.
        self.bars = Bars(selection, config.resolution, config.interval, normalization="raw")
        self.inputs = (self.bars,)
        self.indicators = (
            Indicator("fast", "ema", config.fast, selection, config.resolution, config.interval,
                      normalization="raw"),
            Indicator("slow", "ema", config.slow, selection, config.resolution, config.interval,
                      normalization="raw"),
        )
        # Periods and cadence define the score's meaning for aggregation and research.
        self.calibration_id = (
            f"ema-spread:{config.fast}:{config.slow}:{self.bars.period.total_seconds()}:raw:v1"
        )
        self._current = None

    def update(self, ctx, data, views):
        bar = data.bars.get(self.instrument)
        if bar is None:
            # A slice without this instrument's bar (a dividend, say) restates the view.
            return self._current
        fast, slow = ctx.indicator("fast", self.instrument), ctx.indicator("slow", self.instrument)
        if not (fast.is_ready and slow.is_ready) or fast.time != slow.time or slow.value <= 0:
            raise NotReady("EMA score needs two ready, aligned averages and a positive slow EMA")
        end = horizon_end(ctx, self.instrument, self.config.horizon)
        self._current = {
            "views": (
                ScoreView(
                    return_target(self.instrument, Interval(ctx.time, end)),
                    Score(fast.value / slow.value - 1, self.calibration_id),
                    Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, end),
                ),
            )
        }
        return self._current


class LongWhenPositive:
    """Example allocation rule: a positive score targets a fixed NAV fraction."""

    ports = {"views": InputSpec((ScoreView,), min_count=1, max_count=1)}
    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def __init__(self, weight=0.2):
        if not 0 < weight <= 1:
            raise ContractError("Weight must be in (0, 1]")
        self.weight = weight
        self.config = {"weight": weight}

    def update(self, ctx, data, views):
        forecast = views["views"][0]
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time,
            forecast.metadata.expires_at, (forecast.metadata.view_id,),
        )
        # Weight is a fraction of account NAV; zero is an explicit exit instruction.
        target = PositionView(
            {forecast.target.subject: self.weight if forecast.statement.value > 0 else 0},
            "weight", "ema", metadata,
        )
        return {"positions": (target,)}


def build_graph(instrument, config=EMAConfig()):
    """Score -> long/cash target -> account check; a trading graph."""
    graph = Graph().add_node("signal", EMAScore(instrument, config))
    graph.add_node("allocation", LongWhenPositive(config.weight))
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    graph.connect("signal", "views", "allocation", "views")
    graph.connect("allocation", "positions", "constraints", "positions")
    return graph.set_output("constraints", "positions").compile()


def build_forecast_graph(instrument, config=EMAConfig()):
    """The score alone; a forecast run archives its views instead of trading."""
    return Graph().add_node("signal", EMAScore(instrument, config)).set_output(
        "signal", "views"
    ).compile()
