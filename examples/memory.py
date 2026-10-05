"""Run with python -m examples.memory. Explicit fixture fills are not simulated markets."""

# Aware UTC times make the fixture's decision and expiry boundaries explicit.
from datetime import datetime, timedelta, timezone

# Memory supplies observations and order feedback that the caller controls completely.
from sugarloaf.adapters.memory.gateway import MemoryGateway
from sugarloaf.adapters.memory.services import MemoryServices
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.services import AccountSnapshot, PortfolioConstraints
from sugarloaf.core.types import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.execution import ExecutionEngine, TradingSession
from sugarloaf.market.contracts import Instrument, Trigger
from sugarloaf.nodes.positions import PortfolioConstraintNode, PositionNetter


class FixedContribution:
    """Small authoring example: a desired NAV fraction, not an incremental order."""

    # A scheduled source node can issue a target without prices or upstream forecasts.
    inputs = {}
    outputs = {"positions": OutputSpec((PositionView,))}
    requirements = ()
    triggers = (Trigger("scheduled", key="rebalance"),)

    def __init__(self, instrument, weight, scope):
        self.instrument, self.weight, self.scope = instrument, weight, scope

    def evaluate(self, inputs, ctx):
        # This is an absolute desired position, valid for one hour of logical data time.
        # Snapshot identity makes a second issuance distinct from replaying the same batch.
        metadata = Metadata(
            f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, ctx.now + timedelta(hours=1)
        )
        return NodeOutputs(
            {
                "positions": (
                    PositionView({self.instrument: self.weight}, "weight", self.scope, metadata),
                )
            }
        )


def build_graph(instrument):
    graph = Graph()
    # Both branches manage the same asset: 20% + 30% becomes a 50% account target.
    graph.add_node("left", FixedContribution(instrument, 0.2, "left"))
    graph.add_node("right", FixedContribution(instrument, 0.3, "right"))
    # The 40% branch budgets are ceilings, not multipliers on the requested weights.
    graph.add_node("net", PositionNetter({"left": 0.4, "right": 0.4}))
    # Feasibility is checked after combining overlapping contributions.
    graph.add_node("constraints", PortfolioConstraintNode(PortfolioConstraints()))
    # Netter input names match branch scopes, retaining attribution through composition.
    graph.connect("left", "positions", "net", "left")
    graph.connect("right", "positions", "net", "right")
    graph.connect("net", "positions", "constraints", "positions")
    # The execution session sees only the final account-scoped position output.
    return graph.set_output("constraints", "positions").compile()


def run():
    now = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)
    # Stable identity is separate from the display ticker EXAMPLE.
    instrument = InstrumentId("fixture:equity:1")
    services = MemoryServices(
        (Instrument(instrument, "EXAMPLE"),),
        # Flat USD10,000 account, USD100 mark: a 50% target means 50 shares.
        AccountSnapshot({}, 10000, {"USD": 10000}, {instrument: 100}),
        # Supply an open session explicitly so regular-session execution is admissible.
        calendars={instrument: {"regular": ((now, now + timedelta(hours=6)),)}},
    )
    gateway = MemoryGateway(services)
    execution = ExecutionEngine(gateway)
    # Fixture fills update account state and feed the same engine ledger used for orders.
    gateway.on_update = execution.on_order_update
    session = TradingSession(build_graph(instrument), services, execution)
    # Sequence 1 is one immutable decision boundary with the node's named trigger.
    batch = EventBatch(1, now, (Event("scheduled", key="rebalance"),))
    session.process(batch)
    # The fixture supplies a partial fill; no market simulator decides price or timing.
    gateway.fill("1", 20)
    session.process(batch)  # Exact replay does not accumulate target or order exposure.
    # Holdings of 20 plus the remaining pending 30 already satisfy the 50-share target.
    execution.reconcile()
    gateway.fill("1", 30)
    # With 50 filled and no pending remainder, reconciliation needs no additional order.
    execution.reconcile()
    return {
        # These summarize controlled bookkeeping, not a strategy's market performance.
        "holdings": services.account.holdings[instrument],
        "orders": len(gateway.orders),
        "diagnostics": len(session.runtime.diagnostics),
    }


if __name__ == "__main__":
    print(run())
