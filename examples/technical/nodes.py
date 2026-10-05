"""Three direct-position nodes using completed bars and explicit exit targets."""

# Timedeltas express completed-bar duration and target authority in calendar time.
from datetime import timedelta
from math import isfinite

from sugarloaf.core.errors import ContractError
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeInputs, NodeOutputs, OutputSpec
from sugarloaf.core.services import Context, PortfolioConstraints
from sugarloaf.core.types import Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.market.contracts import Bar, BarsRequirement, Selection, Trigger
from sugarloaf.nodes.positions import PortfolioConstraintNode

# These are portable batch references, separate from adapter-owned streaming indicators.
from .indicators import ema, rsi


def _period(value: int) -> int:
    # Prevent bool and fractional periods from changing a history declaration ambiguously.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("Periods must be positive integers")
    return value


class _BarPositions:
    # Direct-position nodes bypass predictive aggregation and expose one trading output.
    inputs = {}
    outputs = {"positions": OutputSpec((PositionView,))}

    def __init__(
        self,
        instruments,
        lookback,
        *,
        weight=0.1,
        scope="technical",
        interval=timedelta(days=1),
        lifetime=timedelta(days=4),
    ):
        self.instruments = tuple(instruments)
        # Weight is per asset, not a budget divided by the number of active assets.
        if not self.instruments or not scope or not isfinite(weight) or not 0 < weight <= 1:
            raise ContractError("Supply instruments, a scope and a weight in (0, 1]")
        if len(self.instruments) * weight > 1 + 1e-10 or lifetime <= timedelta(0):
            raise ContractError("Total possible weight must be <= 1; lifetime must be positive")
        self.weight, self.scope, self.lifetime = weight, scope, lifetime
        # The Context read must use this exact adjusted-price/history declaration.
        self.bars = BarsRequirement(
            Selection(self.instruments), interval, lookback, normalization="adjusted"
        )
        self.requirements = (self.bars,)
        # Completed observations drive decisions; no timer fabricates missing market bars.
        self.triggers = (Trigger("bar_close", interval=interval),)
        # Desired signal regimes, not broker holdings or a sleeve accounting ledger.
        self._long = {instrument: False for instrument in self.instruments}

    def evaluate(self, inputs: NodeInputs, ctx: Context) -> NodeOutputs:
        # Stage decisions locally so a later failed asset read leaves all signal state intact.
        next_long = {}
        for instrument in self.instruments:
            # Context enforces admission, lookback and selection before delivering history.
            bars = ctx.market.bars(self.bars, instrument)
            next_long[instrument] = self._decide(bars, self._long[instrument], ctx)
        # Explicit zeros exit inactive assets; omission would not mean liquidation.
        positions = {i: self.weight if active else 0.0 for i, active in next_long.items()}
        # Issuance identity is per node/snapshot, and expiry bounds target authority.
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, ctx.now + self.lifetime
        )
        result = NodeOutputs(
            {
                "positions": (
                    PositionView(
                        positions,
                        "weight",
                        # Scope identifies this strategy's branch when targets are netted.
                        self.scope,
                        metadata,
                    ),
                )
            }
        )
        # A failed read for a later instrument must not partially update signal state.
        self._long = next_long
        return result

    def _decide(self, bars: tuple[Bar, ...], was_long: bool, ctx: Context) -> bool:
        raise NotImplementedError


class EMACrossover(_BarPositions):
    """Long while fast EMA > slow EMA; otherwise explicitly target zero.

    This is the crossover regime, including entry when first ready in an existing
    uptrend. It does not require witnessing the crossing event after startup.
    """

    def __init__(self, instruments, fast=20, slow=50, history=None, **kwargs):
        self.fast, self.slow = _period(fast), _period(slow)
        # Extra seed history reduces sensitivity without proving streaming-indicator parity.
        history = 8 * slow if history is None else _period(history)
        if fast >= slow or history < slow:
            raise ContractError("Require fast < slow <= history")
        super().__init__(instruments, history, **kwargs)

    def _decide(self, bars, was_long, ctx):
        closes = tuple(bar.close for bar in bars)
        # Compute caches within this snapshot; it does not preserve the EMA across batches.
        fast = ctx.compute(
            "ema", closes, (self.fast,), "sma-seeded-window-1", lambda: ema(closes, self.fast)
        )
        slow = ctx.compute(
            "ema", closes, (self.slow,), "sma-seeded-window-1", lambda: ema(closes, self.slow)
        )
        # Hold the current trend regime, including startup in an already positive regime.
        # Equality exits; observing a new crossing event is unnecessary.
        return fast > slow


class RSIMeanReversion(_BarPositions):
    """Enter at RSI <= entry; hold until RSI >= exit; long-only, initially flat."""

    def __init__(self, instruments, period=14, entry=30.0, exit=50.0, history=None, **kwargs):
        self.period = _period(period)
        # Wilder smoothing has a different seed policy from an indefinitely running resource.
        history = 12 * period + 1 if history is None else _period(history)
        if not 0 <= entry < exit <= 100 or history < period + 1:
            raise ContractError("Require 0 <= entry < exit <= 100 and period+1 history")
        self.entry, self.exit = entry, exit
        # Separate thresholds create hysteresis: the region between them retains intent.
        super().__init__(instruments, history, **kwargs)

    def _decide(self, bars, was_long, ctx):
        closes = tuple(bar.close for bar in bars)
        value = ctx.compute(
            "rsi",
            closes,
            (self.period,),
            "wilder-flat50-window-1",
            lambda: rsi(closes, self.period),
        )
        # Oversold starts a long intent; recovering to the exit threshold clears it.
        # was_long is the node's regime memory, irrespective of actual fill state.
        return value < self.exit if was_long else value <= self.entry


class DonchianBreakout(_BarPositions):
    """Enter above prior N-bar highs; exit below prior M-bar lows; otherwise hold."""

    def __init__(self, instruments, entry_period=20, exit_period=10, **kwargs):
        self.entry_period, self.exit_period = _period(entry_period), _period(exit_period)
        # One extra bar separates today's close from both previous-bar channels.
        super().__init__(instruments, max(entry_period, exit_period) + 1, **kwargs)

    def _decide(self, bars, was_long, ctx):
        # Exclude the current bar: including its high makes close > upper impossible.
        previous, close = bars[:-1], bars[-1].close
        if was_long:
            # At the lower boundary remain long; only a strict breach requests exit.
            return close >= min(bar.low for bar in previous[-self.exit_period :])
        # Entry requires a strict new high relative to the preceding entry window.
        return close > max(bar.high for bar in previous[-self.entry_period :])


def build_graph(node: _BarPositions):
    """A sole strategy branch followed by account-level target validation."""
    # The supplied node owns the rule; the shared final node checks account feasibility.
    graph = Graph().add_node("signal", node)
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    graph.connect("signal", "positions", "constraints", "positions")
    # A trading graph exposes exactly the validated PositionView, not intermediate signals.
    return graph.set_output("constraints", "positions").compile()
