"""Host-level composition of graph decisions and strategy execution."""

from sugarloaf.core.events import EventBatch, LifecycleEvent
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

        Lifecycle facts invalidate execution before graph processing. Missing or
        multiple terminal Views invalidate intentions and retain filled holdings.
        Exceptions also invalidate execution and propagate to the host.
        """
        # This is the shared bridge from an admitted market batch to order authority.
        # Execution receives the terminal graph target, not every intermediate node output.
        runtime = self.runtime
        # An identical replay must not cancel or resubmit an existing intention.
        # The original arrival is the replay key. Returning early preserves existing
        # order intentions rather than canceling/resubmitting them for duplicate input.
        if batch == runtime._last:
            return runtime.store
        try:
            # Corporate actions can make old targets unsafe before the graph has computed
            # a fresh replacement; revoke authority before consuming changed price/holdings bases.
            if any(isinstance(event.payload, LifecycleEvent) for event in batch.events):
                self.execution.invalidate()
            store = runtime.process(batch)
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
