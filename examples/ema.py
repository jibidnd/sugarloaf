"""One QC-native indicator node, usable alone in research or in a trading graph.

See examples/EMA_RESEARCH.md for source, assumptions and validation evidence.
"""

# Immutable settings can be shared; graph/node instances must be fresh for each run.
from dataclasses import dataclass
from datetime import timedelta

# These contracts keep the signal independent of QC objects and order APIs.
from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import InputSpec, NodeOutputs, OutputSpec
from sugarloaf.core.primitives import Interval, Metadata
from sugarloaf.core.views import PositionView, Score, ScoreView, return_target
from sugarloaf.market.contracts import (
    BarsRequirement,
    IndicatorRequirement,
    InstrumentRequirement,
    Selection,
    Trigger,
)
from sugarloaf.nodes.positions import PortfolioConstraintNode

# Shared horizon arithmetic: calendar time, or a count of exchange sessions.
from sugarloaf.nodes.technical import Horizon, check_horizon, horizon_end
from sugarloaf.portfolio.constraints import PortfolioConstraints


@dataclass(frozen=True)
class EMAConfig:
    # Periods count source bars, so a five-minute deployment uses 20/50 five-minute bars.
    fast: int = 20
    slow: int = 50
    interval: timedelta = timedelta(days=1)
    # How far ahead the score looks, and how long its View stays valid.
    # An int counts trading sessions: 1 means "until the next regular close", so a
    # Friday-close forecast ends at Monday's close, where a price exists to score it
    # and the market is open to trade it. A timedelta is elapsed calendar time
    # instead, which from a Friday close would end on Saturday.
    horizon: Horizon = 1
    weight: float = 0.2

    def __post_init__(self):
        # Reject inconsistent teaching settings before installing provider resources.
        if not 0 < self.fast < self.slow or not 0 < self.weight <= 1:
            raise ContractError("Require 0 < fast < slow and weight in (0, 1]")
        check_horizon(self.horizon)


class EMAScore:
    """Fast/slow EMA spread as a score, not a calibrated return estimate."""

    # This source node needs market services, rather than upstream forecast inputs.
    inputs = {}
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instrument, config=EMAConfig()):
        self.instrument, self.config = instrument, config
        # Declare the native input convention explicitly; this example uses raw prices.
        source = BarsRequirement(Selection((instrument,)), config.interval, normalization="raw")
        # The provider owns EMA calculations/readiness and retains two output points.
        # These declarations grant indicator access without granting a separate raw-bar read.
        self.fast = IndicatorRequirement(source, "ema", (("period", config.fast),), window=2)
        self.slow = IndicatorRequirement(source, "ema", (("period", config.slow),), window=2)
        self.requirements = (self.fast, self.slow)
        # A session-count horizon reads the exchange calendar through
        # ctx.instruments, and every service read needs a matching declaration.
        if isinstance(config.horizon, int):
            self.requirements += (InstrumentRequirement(Selection((instrument,))),)
        # Periods and cadence define the score's meaning for aggregation/research.
        self.calibration_id = (
            f"ema-spread:{config.fast}:{config.slow}:{config.interval.total_seconds()}:raw:v1"
        )
        # Evaluate on completed source bars, not on every platform callback.
        self.triggers = (Trigger("bar_close", instrument, config.interval),)

    def evaluate(self, inputs, ctx):
        # Context exposes only admitted outputs from the declared native resources.
        # Provider readiness precedes evaluation; this node does not reseed a batch EMA.
        fast = ctx.market.indicator(self.fast, self.instrument).points[-1]
        slow = ctx.market.indicator(self.slow, self.instrument).points[-1]
        # Resource readiness alone does not prove that two observations align.
        if fast.time != slow.time or fast.time > ctx.now or slow.values["value"] <= 0:
            raise NotReady("EMA score needs aligned admitted points and a positive denominator")
        end = horizon_end(ctx, self.instrument, self.config.horizon)
        # The target describes the return interval; the statement is a dimensionless score.
        # A positive ratio-minus-one indicates the fast average is above the slow average.
        view = ScoreView(
            return_target(self.instrument, Interval(ctx.now, end)),
            Score(fast.values["value"] / slow.values["value"] - 1, self.calibration_id),
            # Node + snapshot identifies this issuance, whose authority ends at the horizon.
            Metadata(f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, end),
        )
        return NodeOutputs({"views": (view,)})


class LongWhenPositive:
    """Example allocation rule: positive score targets a fixed NAV fraction."""

    # Exactly one score avoids an implicit choice among competing forecasts.
    inputs = {"views": InputSpec((ScoreView,), min_count=1, max_count=1)}
    outputs = {"positions": OutputSpec((PositionView,))}
    requirements = ()
    triggers = ()

    def __init__(self, weight=0.2):
        if not 0 < weight <= 1:
            raise ContractError("Weight must be in (0, 1]")
        self.weight = weight

    def evaluate(self, inputs, ctx):
        forecast = inputs.by_port["views"][0]
        # Preserve parent identity and expiry so allocation cannot outlive its evidence.
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.snapshot_id}",
            ctx.node_id,
            ctx.now,
            forecast.metadata.expires_at,
            (forecast.metadata.view_id,),
        )
        target = PositionView(
            # Weight is a fraction of account NAV; zero is an explicit exit instruction.
            # Equality chooses cash. This rule does not calibrate or optimize the score.
            {forecast.target.subject: self.weight if forecast.statement.value > 0 else 0},
            "weight",
            "ema",
            metadata,
        )
        return NodeOutputs({"positions": (target,)})


def build_graph(instrument, config=EMAConfig()):
    # Each call constructs fresh state: native score -> long/cash target -> account checks.
    graph = Graph().add_node("signal", EMAScore(instrument, config))
    graph.add_node("allocation", LongWhenPositive(config.weight))
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    # Port names identify contracts: forecast Views first, PositionViews after allocation.
    graph.connect("signal", "views", "allocation", "views")
    graph.connect("allocation", "positions", "constraints", "positions")
    # Trading consumes one terminal PositionView; compile validates the graph wiring.
    return graph.set_output("constraints", "positions").compile()
