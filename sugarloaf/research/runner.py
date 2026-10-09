"""Run an ordinary predictive node without a portfolio or execution simulator."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import Node
from sugarloaf.core.primitives import aware, frozen_map
from sugarloaf.core.services import Context
from sugarloaf.market.contracts import ResourceUpdate, Schedule
from sugarloaf.research.archive import ForecastArchive
from sugarloaf.runtime import GraphRuntime


# The runner needs admission/readiness services, not a brokerage or trading host.
# Concrete providers own retrieval, initialization and historical clock assumptions.
class ResearchProvider(Protocol):
    """Structural services required by ``run_node`` and its GraphRuntime.

    Runtime construction installs declarations before ``prepare`` silently warms
    resources. Yield one historical batch at a time; the runtime admits it with
    ``apply`` before requesting the next. Reads expose only admitted information,
    and each provider/run owns fresh state. Unsupported declarations fail visibly.

    ``apply`` may return facts the provider itself discovered during admission
    (or None when it has none); the runtime adds them to the batch. Optional
    ``provenance`` is a mapping recorded by the runner. Outcome anchors are a
    separate interface and no account or execution capability is required.
    """

    @property
    def resources(self) -> Mapping[str, ResourceUpdate]:
        """Current complete declarations, keyed by installed node ID."""
        ...

    def install(self, node_id: str, update: ResourceUpdate) -> None:
        """Replace one node's complete declarations before preparation or batch admission."""
        ...
    def apply(self, batch: EventBatch) -> Iterable[Event] | None:
        """Admit one historical batch, invalidate prior Contexts, return discovered facts."""
        ...
    def context(self, batch: EventBatch | None = None, node_id: str = "") -> Context:
        """Return lazy reads limited to the node's declarations and admitted boundaries."""
        ...
    def ready(self, node_id: str, ctx: Context) -> None:
        """Raise NotReady or CapabilityError when declared resources cannot be consumed."""
        ...
    def schedule_due(
        self, schedule: Schedule, after: datetime | None, through: datetime
    ) -> bool:
        """Test whether a scheduled occurrence crosses (after, through] on graph time."""
        ...
    def prepare(self, start: datetime, end: datetime) -> None:
        """Warm declared resources through start without issuing strategy decisions."""
        ...
    def history_batches(self, start: datetime, end: datetime) -> Iterable[EventBatch]:
        """Yield causal batches individually; apply each batch before requesting another."""
        ...


@dataclass(frozen=True)
class ResearchRun:
    """Issued forecasts, runtime diagnostics, and declared experiment provenance."""

    archive: ForecastArchive
    runtime: GraphRuntime
    provenance: Mapping[str, object]


def run_node(
    node_factory: Callable[[], Node],
    services: ResearchProvider,
    *,
    start: datetime,
    end: datetime,
    node_id: str = "signal",
    output: str | None = None,
    configuration: Mapping | None = None,
) -> ResearchRun:
    """Replay a fresh predictive node without an account or execution simulator.

    Args:
        node_factory: Zero-argument factory creating state for this experiment.
        services: Fresh historical provider admitting one batch at a time.
        start: Aware replay start and silent warmup boundary.
        end: Aware replay end, strictly after start.
        node_id: Registered producer identity.
        output: Designated graph port; required when the node has multiple outputs.
        configuration: Caller-supplied experiment settings recorded as provenance.

    Returns:
        Issued predictive archive, runtime diagnostics, and declared provenance.

    Raises:
        ContractError: Dates, output selection, or single-node wiring are invalid.

    All predictive ports are archived. Required upstream inputs need an explicit
    graph. Outcome resolution remains separate and never feeds labels to the node.
    """
    # Explicit aware boundaries avoid silently choosing a timezone or evaluating
    # an empty interval. Preparation may warm inputs but must not issue forecasts.
    aware(start)
    aware(end)
    if end <= start:
        raise ContractError("Research end must follow start")
    # A factory creates per-experiment mutable node state. Reusing a learned node
    # would mix experiments even if the subsequent batch sequence started over.
    node = node_factory()
    # Graph output selection identifies the designated port. The archive callback
    # still receives every validated predictive port emitted by this node.
    if output is None:
        if len(node.outputs) != 1:
            raise ContractError("Choose an output port for a multi-port research node")
        output = next(iter(node.outputs))
    # Compilation checks this one-node graph, including unsatisfied required
    # upstream inputs. Multi-node experiments must construct their graph explicitly.
    graph = Graph().add_node(node_id, node).set_output(node_id, output).compile()
    archive = ForecastArchive()
    # Runtime construction installs requirements before the provider attempts
    # warmup. Archive validated emissions at issue time, not when labels mature.
    runtime = GraphRuntime(graph, services, on_output=archive.record)
    services.prepare(start, end)
    # Advance the iterator only after process() admits and evaluates its current
    # batch. Providers can stage future retrieval without leaking it into Context.
    for batch in services.history_batches(start, end):
        runtime.process(batch)
    # Configuration is caller-supplied evidence, not introspection of arbitrary
    # node internals. Preserve provider assumptions alongside the issued archive;
    # evaluation resolves labels later through an independent outcome interface.
    provenance = {
        "node": f"{type(node).__module__}.{type(node).__qualname__}",
        "provider": f"{type(services).__module__}.{type(services).__qualname__}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "configuration": dict(configuration or {}),
        "provider_details": dict(getattr(services, "provenance", {})),
        "availability": "Historical provider assumptions; not measured live receipt latency",
    }
    return ResearchRun(archive, runtime, frozen_map(provenance))
