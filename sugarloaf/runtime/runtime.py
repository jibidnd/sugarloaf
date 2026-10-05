"""Serialized batches, whole-node snapshots, and conservative invalidation."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from sugarloaf.core.errors import ContractError, NotReady, NumericalError
from sugarloaf.core.events import EventBatch, LifecycleEvent
from sugarloaf.core.graph import CompiledGraph
from sugarloaf.core.node import InputSpec, NodeInputs, NodeOutputs
from sugarloaf.core.views import PositionView, View
from sugarloaf.market.contracts import ResourceUpdate, UniverseRequirement


@dataclass(frozen=True)
class Diagnostic:
    """A node failure code and message tied to an admitted batch sequence."""
    code: str
    node: str
    batch: int
    message: str


class ViewStore:
    """Active node outputs plus an immutable-identity ledger for this run."""

    def __init__(self):
        # Active outputs may expire or be withdrawn; the separate identity history lasts
        # for the run so an old ID cannot later describe different contents.
        self._snapshots = {}
        self._identities = {}

    def get(self, node_id: str, port: str | None = None) -> NodeOutputs | tuple[View, ...]:
        """Read a whole node snapshot, or its View tuple when a port is supplied."""
        # A missing node/port reads as empty output rather than a remembered stale View.
        # Port cardinality/readiness is enforced when a consumer attempts evaluation.
        output = self._snapshots.get(node_id, NodeOutputs({}))
        return output.by_port.get(port, ()) if port is not None else output

    def replace(self, node_id: str, outputs: NodeOutputs) -> bool:
        """Atomically replace a node snapshot after validating immutable identities.

        Returns:
            Whether effective port contents changed, excluding resource declarations.

        Raises:
            ContractError: An identity is duplicated or reused with different contents.
        """
        # Validate the entire new snapshot before publishing it. Consumers see complete
        # port snapshots, not a series of partially applied message edits.
        seen = set()
        for views in outputs.by_port.values():
            for view in views:
                key = view.metadata.view_id
                if key in seen:
                    raise ContractError("Duplicate View identity in snapshot")
                seen.add(key)
                if key in self._identities and self._identities[key] != view:
                    raise ContractError("View identity reused with different contents")
        old = self.get(node_id)
        for views in outputs.by_port.values():
            for view in views:
                self._identities[view.metadata.view_id] = view
        self._snapshots[node_id] = outputs
        return old.by_port != outputs.by_port

    def withdraw(self, node_id: str) -> bool:
        """Remove all active outputs for a node while retaining its identity history."""
        # Withdrawal removes authority/evidence, not actual broker holdings. A strategy
        # that wants to sell must publish explicit zero positions rather than disappear.
        return self.replace(node_id, NodeOutputs({}))

    def expire(self, cutoff: datetime) -> set[str]:
        """Remove expired Views and return nodes whose effective outputs changed."""
        # Expiry is exclusive: a View ending at this graph cutoff is already unusable.
        # Return affected producers so dependent nodes can recompute after losing inputs.
        changed = set()
        for node, output in tuple(self._snapshots.items()):
            ports = {
                p: tuple(v for v in vs if v.metadata.expires_at > cutoff)
                for p, vs in output.by_port.items()
            }
            if self.replace(node, NodeOutputs(ports)):
                changed.add(node)
        return changed


class GraphRuntime:
    """Evaluate fresh graph state on data arrivals without attaching execution.

    Args:
        graph: Compiled wiring and node instances dedicated to this run.
        services (ProviderServices | ResearchProvider): Provider installing declarations and
            admitting causal snapshots.
        on_output: Optional callback for each successful emission, including unchanged
            output; receives node ID, NodeOutputs, and the normalized batch.

    Construction installs all node resources. Diagnostics are bounded; identity
    history persists for the run. No persistent restart/recovery is supplied.
    """

    def __init__(
        self,
        graph: CompiledGraph,
        services,
        on_output: Callable[[str, NodeOutputs, EventBatch], None] | None = None,
    ):
        # Each node instance belongs to this run. Installing declarations here lets
        # the provider create required resources before the first evaluation.
        self.graph, self.services = graph, services
        self.on_output = on_output
        self.store = ViewStore()
        self.diagnostics = deque(maxlen=2000)
        self.diagnostic_count = 0
        # Replay compares the original arrival; evaluation uses normalized graph time.
        self._last: EventBatch | None = None
        self.batch: EventBatch | None = None
        self._pending = {}
        self._processing = False
        self._invalidated_ids = set()
        self._basis_changes = {}
        for node_id in graph.topological_order:
            node = graph.nodes[node_id]
            services.install(
                node_id, ResourceUpdate(tuple(node.requirements), tuple(node.triggers))
            )

    def _descendants(self, nodes):
        # Propagate invalidation through the graph without relying on every consumer
        # to have its own market trigger.
        result = set(nodes)
        pending = list(nodes)
        while pending:
            for node in self.graph.consumers(pending.pop()):
                if node not in result:
                    result.add(node)
                    pending.append(node)
        return result

    def _triggered(self, node, batch):
        # Schedules test whether a due time was crossed between graph ticks; there is
        # no separate sleeping timer for each node. Many crossed occurrences coalesce.
        triggers = self.services.resources[node].triggers
        after = self.batch.cutoff if self.batch is not None else None
        for trigger in triggers:
            if trigger.schedule is not None:
                if self.services.schedule_due(trigger.schedule, after, batch.cutoff):
                    return True
            elif any(
                trigger.kind == event.kind
                and (trigger.instrument is None or trigger.instrument == event.instrument)
                and (trigger.interval is None or trigger.interval == event.interval)
                and (trigger.key is None or trigger.key == event.key)
                for event in batch.events
            ):
                return True
        return False

    def process(self, batch: EventBatch) -> ViewStore:
        """Admit one arrival and evaluate activated nodes once in topological order.

        Exact replay of the last completed arrival is a no-op. Graph time never
        rewinds. Resource replacements become effective before admission; expired
        or lifecycle-invalidated Views are withdrawn before evaluation.

        Args:
            batch: Ordered arrival with separate observation and receipt boundaries.

        Returns:
            Current active ViewStore after output replacement and Context release.

        Raises:
            ContractError: Processing is reentrant, sequence/receipt regresses, or
                outputs violate the graph contract.

        NotReady and NumericalError withdraw the node output and record diagnostics.
        Other evaluation exceptions withdraw descendants and propagate. Processing
        does not roll back provider or node state on failure.
        """
        # Hosts must serialize arrivals. Distinct receipt/sequence identities may share
        # an observation cutoff; equal observation time does not imply duplicate input.
        if self._processing:
            raise ContractError("Graph batches must be serialized")
        if self._last is not None:
            if batch == self._last:
                return self.store
            if batch.sequence <= self._last.sequence or batch.received_at < self._last.received_at:
                raise ContractError("Batch sequence/receipt regressed or replay conflicts")
        input_batch = batch
        ends = [event.end for event in batch.events if event.end is not None]
        # Observation ends establish graph time; receipt time controls data availability
        # separately. A late observation is admitted without rewinding the prior clock.
        clock = max(ends) if ends else batch.cutoff
        if self.batch is not None:
            clock = max(clock, self.batch.cutoff)
        batch = replace(batch, cutoff=clock)
        self._processing = True
        contexts = []
        try:
            # Declarations emitted in the previous batch become effective before
            # this admission; a node cannot change resources halfway through a batch.
            resources_changed = set(self._pending)
            for node, update in self._pending.items():
                self.services.install(node, update)
            self._pending.clear()
            self.services.apply(batch)
            extra = tuple(getattr(self.services, "additional_events", ()))
            if extra:
                batch = replace(batch, events=batch.events + extra)
            # Expire before evaluation so a consumer cannot use inputs whose authority
            # already ended at the newly admitted observation boundary.
            expired = self.store.expire(batch.cutoff)
            dirty = self._descendants(
                {consumer for node in expired for consumer in self.graph.consumers(node)}
            )
            dirty.update(resources_changed)
            dirty.update(n for n in self.graph.nodes if self._triggered(n, batch))
            # Membership changes activate both owning nodes and nodes reading that named
            # selection, even if their usual price/schedule trigger did not fire.
            changed_universes = {event.key for event in batch.events if event.kind == "universe"}
            changed_selections = changed_universes - {None}
            for node_id, update in self.services.resources.items():
                for requirement in update.requirements:
                    owns_changed_universe = (
                        isinstance(requirement, UniverseRequirement)
                        and requirement.definition.name in changed_universes
                    )
                    selection = getattr(requirement, "selection", None)
                    reads_changed_universe = (
                        getattr(selection, "universe", None) in changed_selections
                    )
                    if owns_changed_universe or reads_changed_universe:
                        dirty.add(node_id)
                        break
            lifecycle = any(isinstance(e.payload, LifecycleEvent) for e in batch.events)
            # Lifecycle events may change adjustment bases or holdings without bars.
            # Conservatively invalidate every branch before anything reaches execution.
            if lifecycle:
                dirty.update(self.graph.nodes)
                self._invalidate_lifecycle(batch)
            # Topological order makes each consumer see its producers' replacements
            # from this batch. A node is evaluated at most once, even with many triggers.
            for node_id in self.graph.topological_order:
                if node_id not in dirty:
                    continue
                node = self.graph.nodes[node_id]
                # Gather current upstream Views from every connected edge. Input tuples follow
                # edge registration order; evaluation follows producer-before-consumer order.
                ports = {p: () for p in node.inputs}
                for edge in self.graph.incoming(node_id):
                    ports[edge.destination_port] += self.store.get(
                        edge.source_node, edge.source_port
                    )
                ctx = self.services.context(batch, node_id)
                contexts.append(ctx)
                try:
                    for port, spec in node.inputs.items():
                        spec.validate(ports[port])
                    # Data declarations are permissions; readiness additionally proves that this
                    # snapshot has enough timely observations and ready membership/resources.
                    self.services.ready(node_id, ctx)
                    # This is the user node call. It reads only the supplied snapshot and returns
                    # a complete output snapshot, possibly with declarations for the next batch.
                    output = node.evaluate(NodeInputs(ports), ctx)
                    self._validate(node_id, output, batch)
                    changed = self.store.replace(node_id, output)
                    if self.on_output is not None:
                        self.on_output(node_id, output, batch)
                    # Defer declaration changes: an evaluation cannot retroactively subscribe to
                    # new data halfway through the snapshot it is currently reading.
                    if output.resources is not None:
                        self._pending[node_id] = output.resources
                # Expected unavailability/calculation failures withdraw output and are diagnosed.
                # Keeping yesterday's forecast here would hide a missing or invalid new result.
                except (NotReady, NumericalError) as error:
                    changed = self.store.withdraw(node_id)
                    self.diagnostic_count += 1
                    self.diagnostics.append(
                        Diagnostic(error.code, node_id, batch.sequence, str(error))
                    )
                except Exception:
                    for affected in self._descendants({node_id}):
                        self.store.withdraw(affected)
                    raise
                if changed:
                    dirty.update(self.graph.consumers(node_id))
            # Remember the original arrival for exact replay while retaining the normalized
            # batch separately as the current shared graph clock.
            self._last = input_batch
            self.batch = batch
            return self.store
        finally:
            # Release every created Context, including those belonging to failed evaluations.
            # Nodes must store derived values/IDs, not these guarded service handles.
            for ctx in contexts:
                ctx.close()
            self._processing = False

    def _invalidate_lifecycle(self, batch: EventBatch) -> None:
        """Withdraw outputs and prevent reuse of identities or older price bases."""
        # Corporate actions change the meaning/basis of prior values. Retain a boundary
        # per instrument and prohibit reusing outputs created against the old basis.
        for event in batch.events:
            if isinstance(event.payload, LifecycleEvent):
                instrument = event.payload.instrument
                effective = event.payload.effective_at
                # A late older action cannot lower the last known basis boundary.
                self._basis_changes[instrument] = max(
                    effective, self._basis_changes.get(instrument, effective)
                )
        for node in self.graph.nodes:
            self._invalidated_ids.update(
                view.metadata.view_id
                for views in self.store.get(node).by_port.values()
                for view in views
            )
            self.store.withdraw(node)

    def _validate(self, node_id, output, batch):
        # Validate actual emitted messages as well as compile-time port declarations.
        # Only the registered producer can emit its IDs with valid time/provenance.
        if not isinstance(output, NodeOutputs):
            raise ContractError("Node.evaluate must return NodeOutputs")
        node = self.graph.nodes[node_id]
        if set(output.by_port) - set(node.outputs):
            raise ContractError("Undeclared output port")
        for port, views in output.by_port.items():
            spec = node.outputs[port]
            InputSpec(
                spec.emits,
                min_count=0,
                statements=spec.statements,
                quantities=spec.quantities,
                properties=spec.properties,
            ).validate(views)
            for view in views:
                if view.metadata.view_id in self._invalidated_ids:
                    raise NotReady("Lifecycle-invalidated View identity cannot be reused")
                subjects = set(view.positions) if isinstance(view, PositionView) else set()
                target = getattr(view, "target", None)
                if target is not None:
                    subject = target.subject
                    subjects.update(subject if isinstance(subject, tuple) else (subject,))
                if any(
                    view.metadata.cutoff < self._basis_changes[instrument]
                    for instrument in subjects
                    if instrument in self._basis_changes
                ):
                    raise NotReady("View information cutoff predates instrument lifecycle change")
                if (
                    view.metadata.source != node_id
                    or view.metadata.cutoff > batch.cutoff
                    or view.metadata.expires_at <= batch.cutoff
                ):
                    raise ContractError("Output source, cutoff or expiry is invalid")
