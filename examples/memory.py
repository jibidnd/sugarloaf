"""Run with python -m examples.memory.

The memory adapter fills every order at the price set here, with no fee or slippage.
"""

from datetime import UTC, datetime, time, timedelta

from sugarloaf.adapters.memory import MemoryAdapter
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import Schedule
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.execution import ExecutionPolicy
from sugarloaf.nodes.positions import PortfolioConstraintNode, PositionNetter
from sugarloaf.portfolio.constraints import PortfolioConstraints
from sugarloaf.runtime import GraphRunner


class FixedContribution:
    """Small authoring example: a desired NAV fraction issued on a schedule."""

    ports = {}
    outputs = {"positions": OutputSpec((PositionView,))}
    inputs = ()

    def __init__(self, instrument, weight, scope):
        self.instrument, self.weight, self.scope = instrument, weight, scope
        # One named schedule per node; the platform fires it, the runner routes it.
        self.schedules = (Schedule(f"rebalance-{scope}", at=time(10, 0)),)
        self.config = {"weight": weight}

    def on_scheduled(self, ctx, name, views):
        # An absolute desired position, valid for one hour of platform time.
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, ctx.time + timedelta(hours=1)
        )
        target = PositionView({self.instrument: self.weight}, "weight", self.scope, metadata)
        return {"positions": (target,)}


def build_graph(instrument):
    graph = Graph()
    # Both branches manage the same asset: 20% + 30% becomes a 50% account target.
    graph.add_node("left", FixedContribution(instrument, 0.2, "left"))
    graph.add_node("right", FixedContribution(instrument, 0.3, "right"))
    # The 40% branch budgets are ceilings, not multipliers on the requested weights.
    graph.add_node("net", PositionNetter({"left": 0.4, "right": 0.4}))
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    graph.connect("left", "positions", "net", "left")
    graph.connect("right", "positions", "net", "right")
    graph.connect("net", "positions", "constraints", "positions")
    # Execution sees only the final account-scoped position output.
    return graph.set_output("constraints", "positions").compile()


def run():
    now = datetime(2024, 1, 2, 15, tzinfo=UTC)
    instrument = InstrumentId("example:equity:1")
    # Flat USD10,000 account, USD100 mark: a 50% target means 50 shares.
    adapter = MemoryAdapter(now, account=10_000)
    adapter.set_price(instrument, 100.0)
    runner = GraphRunner(build_graph(instrument), adapter, policy=ExecutionPolicy(rebalance_band=0))
    adapter.attach(runner)
    adapter.fire("rebalance-left")
    # One branch alone is not a complete target: the netter waits for both.
    assert runner.last_target is None
    adapter.fire("rebalance-right")
    # The same target restated does not re-order.
    adapter.fire("rebalance-right")
    return {
        "holdings": adapter.holdings[instrument],
        "orders": len(runner.orders),
        "diagnostics": runner.diagnostic_count,
    }


if __name__ == "__main__":
    print(run())
