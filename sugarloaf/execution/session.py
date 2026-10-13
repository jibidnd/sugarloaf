"""Host-level composition of graph decisions and strategy execution."""

from sugarloaf.core.events import RESCALING, EventBatch, LifecycleEvent
from sugarloaf.core.graph import CompiledGraph
from sugarloaf.core.views import PositionView
from sugarloaf.runtime import GraphRuntime
from sugarloaf.runtime.runtime import ViewStore


class TradingSession:
    """Compose graph decisions with execution and conservative target invalidation.

    Args:
        graph: Fresh compiled graph declaring one terminal PositionView output.
        services (ProviderServices): Causal data provider used by GraphRuntime.
        execution (ExecutionEngine): Engine accepting position targets and invalidating intentions.
        on_output (Callable[[str, NodeOutputs, EventBatch], None] | None): Optional callback
            observing validated node emissions.

    Hosts serialize order feedback around processing. Predictive research instead
    uses GraphRuntime directly and requires no execution component.
    """

    def __init__(self, graph: CompiledGraph, services, execution, *, on_output=None):
        # Research can use GraphRuntime alone. Trading additionally requires a graph
        # whose designated output represents desired positions and an execution engine.
        graph.require_positions()
        self.runtime = GraphRuntime(graph, services, on_output=on_output)
        self.execution = execution

    def process(self, batch: EventBatch) -> ViewStore:
        """Process a batch and authorize exactly one terminal PositionView.

        Args:
            batch: Serialized market arrival; identical completed replay is ignored.

        Returns:
            Current graph ViewStore.

        Missing or multiple terminal Views invalidate intentions and retain filled
        holdings. That includes a corporate action: the runtime withdraws the
        Views it affects, so an affected target disappears here and its orders are
        cancelled, while a target that does not involve the instrument carries on.
        A weight target survives a split; only its waiting orders in the split
        instrument are cancelled, and they are sized again.
        Exceptions also invalidate execution and propagate to the host.
        """
        # This is the shared bridge from an admitted market batch to order authority.
        # Execution receives the terminal graph target, not every intermediate node output.
        runtime = self.runtime
        # An identical replay must not cancel or resubmit an existing intention.
        # Returning early preserves existing order intentions rather than
        # canceling/resubmitting them for duplicate input.
        if runtime.is_replay(batch):
            return runtime.store
        try:
            store = runtime.process(batch)
            # A split leaves a weight target standing, but orders already waiting
            # in that instrument were counted in the old shares. Cancel them; the
            # reconciliation below sizes again from fresh holdings and prices.
            for event in batch.events:
                fact = event.payload
                if isinstance(fact, LifecycleEvent) and fact.kind in RESCALING:
                    self.execution.cancel_orders(fact.instrument)
            # A compiled PositionView declaration is necessary but insufficient: each batch
            # must actually leave exactly one usable terminal target in the ViewStore.
            outputs = store.get(*runtime.graph.output)
            if len(outputs) != 1 or not isinstance(outputs[0], PositionView):
                self.execution.invalidate()
            else:
                # The engine obtains fresh authoritative state from its gateway.
                self.execution.accept(outputs[0])
            return store
        # A failing graph cannot authorize stale trading. Request invalidation before
        # propagating; cancellation can itself fail. Filled holdings are not sold here.
        except Exception:
            self.execution.invalidate()
            raise
