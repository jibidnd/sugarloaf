"""Serialized batches, whole-node snapshots, and conservative invalidation."""

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from sugarloaf.core.errors import ContractError, NotReady, NumericalError
from sugarloaf.core.events import EventBatch, LifecycleEvent
from sugarloaf.core.graph import CompiledGraph
from sugarloaf.core.node import InputSpec, NodeInputs, NodeOutputs
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.views import PositionView, View
from sugarloaf.market.contracts import (
    BarsRequirement,
    QuotesRequirement,
    ResourceUpdate,
    UniverseRequirement,
    restates_history,
)

# Ledger marker for an identity that a lifecycle event invalidated. It is a
# distinct object so it can never compare equal to a real View.
_RETIRED = object()


def _subjects(view: View) -> set:
    """Return every subject a View makes a claim about or requests a position in."""
    # A PositionView names instruments through its positions; predictive Views
    # name one subject or a tuple of them through their Target.
    subjects = set(view.positions) if isinstance(view, PositionView) else set()
    target = getattr(view, "target", None)
    if target is not None:
        subject = target.subject
        subjects.update(subject if isinstance(subject, tuple) else (subject,))
    return subjects


@dataclass(frozen=True)
class Diagnostic:
    """A node failure code and message tied to an admitted batch sequence."""
    code: str
    node: str
    batch: int
    message: str


class ViewStore:
    """Active node outputs plus a bounded ledger of recently issued identities.

    Args:
        identity_limit: How many of the most recently issued or re-issued View
            identities the ledger remembers.

    The ledger enforces that one identity never describes two different
    contents. It remembers the latest ``identity_limit`` identities rather than
    every identity of the run: a node normally derives its IDs from the batch
    sequence, so a clash, if a node has one, happens against a recent identity.
    Keeping every View of a multi-year minute backtest would hold millions of
    objects for a check that only ever looks at the newest few.
    """

    def __init__(self, identity_limit: int = 100_000):
        if identity_limit < 1:
            raise ContractError("Identity ledger must remember at least one View")
        # Active outputs may expire or be withdrawn; the separate identity ledger
        # outlives them so an old ID cannot quietly return with different contents.
        self._snapshots = {}
        # identity -> the View issued under it, or _RETIRED. Ordered oldest first so
        # the least recently used identity is the one dropped at the limit.
        self._identities: OrderedDict = OrderedDict()
        self._identity_limit = identity_limit

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
                known = self._identities.get(key)
                # A retired identity is rejected earlier by the runtime as NotReady;
                # here only a live identity with changed contents is a contract error.
                if known is not None and known is not _RETIRED and known != view:
                    raise ContractError("View identity reused with different contents")
        old = self.get(node_id)
        for views in outputs.by_port.values():
            for view in views:
                self._remember(view.metadata.view_id, view)
        self._snapshots[node_id] = outputs
        return old.by_port != outputs.by_port

    def _remember(self, identity: str, value) -> None:
        """Record an identity as most recently used and drop the oldest beyond the limit."""
        self._identities[identity] = value
        self._identities.move_to_end(identity)
        while len(self._identities) > self._identity_limit:
            self._identities.popitem(last=False)

    def retire(self, node_id: str) -> None:
        """Withdraw a node's outputs and forbid reuse of their identities.

        Used when a lifecycle event changes what those Views meant. A node that
        cached an old View object cannot hand it back later as if still valid.
        """
        for views in self.get(node_id).by_port.values():
            for view in views:
                self._remember(view.metadata.view_id, _RETIRED)
        self.withdraw(node_id)

    def retired(self, identity: str) -> bool:
        """Whether a lifecycle event retired this identity."""
        return self._identities.get(identity) is _RETIRED

    def withdraw(self, node_id: str) -> bool:
        """Remove all active outputs for a node; its identities stay in the ledger."""
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

    Construction installs all node resources. Diagnostics and the identity
    ledger are bounded. No persistent restart/recovery is supplied.
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
        # (node ID, instrument) -> time of the latest lifecycle event that changed
        # that node's basis for that instrument. A View from that node about that
        # instrument must be based on information at or after this time.
        self._basis_changes: dict[tuple[str, InstrumentId], datetime] = {}
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
            elif any(trigger.matches(event) for event in batch.events):
                return True
        return False

    def is_replay(self, batch: EventBatch) -> bool:
        """Whether this is exactly the last arrival that finished processing.

        ``process`` treats such a batch as a no-op. A host that would otherwise act
        on the result, such as TradingSession, checks this first so a duplicated
        callback neither re-evaluates the graph nor touches orders.
        """
        return self._last is not None and batch == self._last

    def process(self, batch: EventBatch) -> ViewStore:
        """Admit one arrival and evaluate activated nodes once in topological order.

        Exact replay of the last completed arrival is a no-op. Graph time never
        rewinds. Resource replacements become effective before admission; expired
        Views, and the Views a lifecycle event affects, are withdrawn before
        evaluation. A lifecycle event touches only the nodes it concerns.

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
        if self.is_replay(batch):
            return self.store
        if self._last is not None:
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
            # Admission can itself discover facts, such as a universe whose
            # membership changed. The provider returns them so nodes can react in
            # this same batch. A provider with nothing to add may return None.
            extra = tuple(self.services.apply(batch) or ())
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
            # Lifecycle events may change adjustment bases or holdings without bars.
            # Withdraw the outputs they affect before anything reaches execution,
            # and re-evaluate those nodes even if their usual trigger did not fire.
            dirty.update(self._invalidate_lifecycle(batch))
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

    def _reads_changed_basis(self, node_id: str, life: LifecycleEvent) -> bool:
        """Whether a node declares price data whose basis this lifecycle fact changes."""
        for requirement in self.services.resources[node_id].requirements:
            # An indicator is computed from its source bars, so judge it by them.
            source = getattr(requirement, "source", requirement)
            if not isinstance(source, (BarsRequirement, QuotesRequirement)):
                continue
            selection = source.selection
            # A named universe might contain the instrument. The runtime cannot
            # resolve membership itself, so it conservatively assumes it does.
            if selection.universe is None and life.instrument not in selection.instruments:
                continue
            if restates_history(life.kind, getattr(source, "normalization", None)):
                return True
        return False

    def _invalidate_lifecycle(self, batch: EventBatch) -> set[str]:
        """Withdraw the outputs a lifecycle event affects and return those nodes.

        Only the affected part of the graph is touched. A dividend on one stock
        of a large universe must not withdraw every other forecast and cancel
        every order, which is what invalidating the whole graph would do.

        A node is affected when either:

        * it declares price data for the instrument whose basis the event changes
          (see ``restates_history``; a dividend leaves raw prices alone), or
        * for any event other than a dividend, one of its active Views names the
          instrument. A split or delisting changes what a quantity or a forecast
          about that instrument means, whatever data the node read.

        Everything downstream of an affected node is affected too, because its
        output was derived from Views that are no longer valid.
        """
        affected: set[str] = set()
        for event in batch.events:
            life = event.payload
            if not isinstance(life, LifecycleEvent):
                continue
            nodes = {n for n in self.graph.nodes if self._reads_changed_basis(n, life)}
            if life.kind != "dividend":
                nodes |= {
                    node
                    for node in self.graph.nodes
                    for views in self.store.get(node).by_port.values()
                    if any(life.instrument in _subjects(view) for view in views)
                }
            nodes = self._descendants(nodes)
            # Record from when each node's Views about this instrument must date.
            # A dividend binds only the affected nodes. Any other event binds every
            # node: after a split, no node may publish a claim about the instrument
            # that rests on information from before it, whether or not it had an
            # active View to withdraw at this moment.
            bound = nodes if life.kind == "dividend" else self.graph.nodes
            for node in bound:
                key = (node, life.instrument)
                # A late older action cannot lower the last known basis boundary.
                self._basis_changes[key] = max(
                    life.effective_at, self._basis_changes.get(key, life.effective_at)
                )
            affected |= nodes
        for node in affected:
            self.store.retire(node)
        return affected

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
                if self.store.retired(view.metadata.view_id):
                    raise NotReady("Lifecycle-invalidated View identity cannot be reused")
                # A fresh identity is not enough: the View must also be based on
                # information from after this node's basis changed for the instrument.
                if any(
                    view.metadata.cutoff < self._basis_changes[node_id, instrument]
                    for instrument in _subjects(view)
                    if (node_id, instrument) in self._basis_changes
                ):
                    raise NotReady("View information cutoff predates instrument lifecycle change")
                if (
                    view.metadata.source != node_id
                    or view.metadata.cutoff > batch.cutoff
                    or view.metadata.expires_at <= batch.cutoff
                ):
                    raise ContractError("Output source, cutoff or expiry is invalid")
