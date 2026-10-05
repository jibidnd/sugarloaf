from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryServices, SelectionRow
from sugarloaf.core.errors import ContractError, NumericalError
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import InputSpec, NodeOutputs, OutputSpec
from sugarloaf.core.types import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.market.contracts import (
    LiquidityUniverse,
    ResourceUpdate,
    Trigger,
    UniverseRequirement,
)
from sugarloaf.runtime import GraphRuntime, ViewStore

NOW = datetime(2025, 1, 2, 15, tzinfo=UTC)
A = InstrumentId("A")


class Source:
    inputs = {}
    outputs = {"p": OutputSpec((PositionView,))}
    requirements = ()
    triggers = (Trigger("data"),)

    def __init__(self):
        self.calls = 0
        self.failure = False
        self.resources = None

    def evaluate(self, inputs, ctx):
        self.calls += 1
        if self.failure:
            raise NumericalError("Controlled solver failure")
        view = PositionView(
            {A: 2},
            "quantity",
            "account",
            Metadata(
                f"{ctx.node_id}:{ctx.snapshot_id}",
                ctx.node_id,
                ctx.now,
                ctx.now + timedelta(minutes=2),
            ),
        )
        return NodeOutputs({"p": (view,)}, self.resources)


class Consumer(Source):
    inputs = {"in": InputSpec((PositionView,))}
    triggers = ()


def graph(source, consumer=None):
    result = Graph().add_node("s", source)
    if consumer:
        result.add_node("c", consumer).connect("s", "p", "c", "in").set_output("c", "p")
    else:
        result.set_output("s", "p")
    return result.compile()


def test_replay_retention_expiry_does_not_refresh_source():
    source, consumer = Source(), Consumer()
    runtime = GraphRuntime(graph(source, consumer), MemoryServices())
    batch = EventBatch(0, NOW, (Event("data"), Event("data")))
    runtime.process(batch)
    original = runtime.store.get("s", "p")[0]
    runtime.process(batch)
    runtime.process(EventBatch(1, NOW + timedelta(minutes=1)))
    assert source.calls == consumer.calls == 1
    assert runtime.store.get("s", "p")[0] is original
    runtime.process(EventBatch(2, NOW + timedelta(minutes=2)))
    assert source.calls == 1
    assert not runtime.store.get("s", "p")
    assert not runtime.store.get("c", "p")
    with pytest.raises(ContractError):
        runtime.process(EventBatch(2, NOW + timedelta(minutes=2), (Event("data"),)))


def test_atomic_replacement_withdrawal_identity_and_numerical_failure():
    source, consumer = Source(), Consumer()
    runtime = GraphRuntime(graph(source, consumer), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    source.failure = True
    runtime.process(EventBatch(1, NOW + timedelta(seconds=1), (Event("data"),)))
    assert not runtime.store.get("s", "p") and not runtime.store.get("c", "p")
    assert {d.code for d in runtime.diagnostics} == {"NUMERICAL_FAILURE", "DATA_NOT_READY"}
    store = ViewStore()
    meta = Metadata("id", "s", NOW, NOW + timedelta(days=1))
    view = PositionView({A: 1}, "quantity", "account", meta)
    assert store.replace("s", NodeOutputs({"p": (view,)}))
    assert not store.replace("s", NodeOutputs({"p": (view,)}))
    with pytest.raises(ContractError):
        store.replace("s", NodeOutputs({"p": (PositionView({A: 2}, "quantity", "account", meta),)}))
    assert store.get("s", "p") == (view,)
    store.replace("s", NodeOutputs({}))
    assert store.get("s", "p") == ()


def test_lifecycle_and_deferred_resource_changes():
    source = Source()
    source.resources = ResourceUpdate((), (Trigger("scheduled", key="new"),))
    services = MemoryServices()
    runtime = GraphRuntime(graph(source), services)
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    assert services.resources["s"].triggers == (Trigger("data"),)
    runtime.process(EventBatch(1, NOW + timedelta(seconds=1)))
    assert services.resources["s"].triggers == (Trigger("scheduled", key="new"),)
    source.resources = None
    source.failure = True
    event = LifecycleEvent("split", A, NOW + timedelta(seconds=2), factor=0.5)
    runtime.process(EventBatch(2, event.effective_at, (Event("lifecycle", A, payload=event),)))
    assert not runtime.store.get("s", "p")


def test_split_cannot_reactivate_cached_old_quantity_target():
    class Cached(Source):
        def evaluate(self, inputs, ctx):
            if not hasattr(self, "cached"):
                self.cached = super().evaluate(inputs, ctx)
            return self.cached

    source = Cached()
    runtime = GraphRuntime(graph(source), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    at = NOW + timedelta(seconds=1)
    runtime.process(
        EventBatch(
            1, at, (Event("lifecycle", A, payload=LifecycleEvent("split", A, at, factor=0.5)),)
        )
    )
    assert runtime.store.get("s", "p") == ()
    assert "identity" in runtime.diagnostics[-1].message


def test_expired_consumer_recomputes_when_one_of_two_inputs_expires():
    class TwoLifetimes(Source):
        def evaluate(self, inputs, ctx):
            self.calls += 1
            return NodeOutputs(
                {
                    "p": tuple(
                        PositionView(
                            {A: 1},
                            "quantity",
                            "account",
                            Metadata(
                                f"source:{minutes}",
                                ctx.node_id,
                                ctx.now,
                                ctx.now + timedelta(minutes=minutes),
                            ),
                        )
                        for minutes in (2, 3)
                    )
                }
            )

    source, consumer = TwoLifetimes(), Consumer()
    runtime = GraphRuntime(graph(source, consumer), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    runtime.process(EventBatch(1, NOW + timedelta(minutes=2)))
    assert source.calls == 1
    assert consumer.calls == 2
    assert len(runtime.store.get("s", "p")) == 1
    assert len(runtime.store.get("c", "p")) == 1


def test_membership_change_rechecks_node_without_its_ordinary_trigger():
    source = Source()
    source.requirements = (
        UniverseRequirement(LiquidityUniverse("u", 1, refresh=Trigger("scheduled", key="select"))),
    )
    services = MemoryServices()
    runtime = GraphRuntime(graph(source), services)
    services.set_selection("u", [SelectionRow(A, 100, 10, 1000)], NOW)
    runtime.process(EventBatch(0, NOW, (Event("scheduled", key="select"),)))
    assert source.calls == 1
    services.set_selection("u", [], NOW + timedelta(seconds=1))
    runtime.process(EventBatch(1, NOW + timedelta(seconds=1), (Event("scheduled", key="select"),)))
    assert source.calls == 2
