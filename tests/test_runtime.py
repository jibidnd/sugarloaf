from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryServices, SelectionRow
from sugarloaf.core.errors import ContractError, NumericalError
from sugarloaf.core.events import DataReset, Event, EventBatch, LifecycleEvent
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import InputSpec, NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import PositionView, Score, ScoreView, return_target
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


class Reader:
    """Emit one score about an instrument and count how often it is evaluated."""

    inputs = {}
    outputs = {"views": OutputSpec((ScoreView,))}
    # No data requirement: these tests are about which nodes the runtime
    # withdraws and re-evaluates, which it decides from facts, not from data.
    requirements = ()
    triggers = (Trigger("data"),)

    def __init__(self, instrument):
        self.instrument = instrument
        self.calls = 0

    def evaluate(self, inputs, ctx):
        self.calls += 1
        end = ctx.now + timedelta(days=1)
        view = ScoreView(
            return_target(self.instrument, Interval(ctx.now, end)),
            Score(1.0, "fixture"),
            Metadata(f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, end),
        )
        return NodeOutputs({"views": (view,)})


def lifecycle_batch(sequence, kind, instrument, **fields):
    at = NOW + timedelta(seconds=sequence)
    event = LifecycleEvent(kind, instrument, at, **fields)
    return EventBatch(sequence, at, (Event("lifecycle", instrument, payload=event),))


def reset_batch(sequence, instrument, nodes):
    """A batch carrying a provider's report that it rebuilt those nodes' data."""
    at = NOW + timedelta(seconds=sequence)
    fact = DataReset(instrument, nodes, at)
    return EventBatch(sequence, at, (Event("data_reset", instrument, payload=fact),))


def test_data_reset_withdraws_only_the_nodes_the_provider_names():
    b = InstrumentId("B")
    nodes = {"rebuilt_a": Reader(A), "other_a": Reader(A), "reads_b": Reader(b)}
    builder = Graph()
    for name, node in nodes.items():
        builder.add_node(name, node)
    runtime = GraphRuntime(builder.set_output("rebuilt_a", "views").compile(), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    before = {name: runtime.store.get(name, "views") for name in nodes}
    assert all(len(views) == 1 for views in before.values())

    # The provider says it rebuilt the data one node declared. Only that node is
    # withdrawn and evaluated again; a second node about the same stock, whose data
    # the provider did not rebuild, keeps its View. A name that is not in this
    # graph is ignored, because a provider may serve more than one consumer.
    runtime.process(reset_batch(1, A, ("rebuilt_a", "not_in_this_graph")))
    assert [node.calls for node in nodes.values()] == [2, 1, 1]
    assert runtime.store.get("rebuilt_a", "views") != before["rebuilt_a"]
    assert runtime.store.get("other_a", "views") == before["other_a"]
    assert runtime.store.get("reads_b", "views") == before["reads_b"]

    # A dividend with no reset reported is only a fact. The runtime holds no rule
    # of its own about dividends, so nothing is withdrawn.
    runtime.process(lifecycle_batch(2, "dividend", A, amount=1.0))
    assert [node.calls for node in nodes.values()] == [2, 1, 1]

    # A split changes what a share of A is, but these are scores about A's
    # return, which mean what they did. Nothing is withdrawn.
    runtime.process(lifecycle_batch(3, "split", A, factor=0.5))
    assert [node.calls for node in nodes.values()] == [2, 1, 1]

    # A delisting ends A, whichever vendor supplies the data. Every View naming
    # it is withdrawn; the other stock is still untouched.
    runtime.process(lifecycle_batch(4, "delisting", A))
    assert [node.calls for node in nodes.values()] == [3, 2, 1]
    assert runtime.store.get("reads_b", "views") == before["reads_b"]


def test_data_reset_also_withdraws_everything_downstream():
    source, consumer = Source(), Consumer()
    runtime = GraphRuntime(graph(source, consumer), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    kept = runtime.store.get("c", "p")
    # Only the source is named, but the consumer's target was derived from the
    # source's withdrawn View, so it must be decided again too.
    runtime.process(reset_batch(1, A, ("s",)))
    assert (source.calls, consumer.calls) == (2, 2)
    assert runtime.store.get("c", "p") != kept


def test_data_reset_must_name_at_least_one_node():
    # A provider that rebuilt nothing reports nothing; an empty report is a bug.
    with pytest.raises(ContractError):
        DataReset(A, (), NOW)


def test_split_withdraws_views_naming_the_instrument_but_a_dividend_does_not():
    # Neither node reads price data, but both of their quantity targets name A.
    source, consumer = Source(), Consumer()
    runtime = GraphRuntime(graph(source, consumer), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    kept = runtime.store.get("c", "p")
    calls = (source.calls, consumer.calls)
    # A cash dividend does not change what "2 shares of A" means: nothing happens.
    runtime.process(lifecycle_batch(1, "dividend", A, amount=1.0))
    assert runtime.store.get("c", "p") == kept
    assert (source.calls, consumer.calls) == calls
    # A split does. Both targets are withdrawn and both nodes decide afresh.
    runtime.process(lifecycle_batch(2, "split", A, factor=0.5))
    assert (source.calls, consumer.calls) == (calls[0] + 1, calls[1] + 1)
    assert runtime.store.get("c", "p") != kept


def test_split_leaves_weight_targets_and_return_claims_standing():
    class Weights(Source):
        def evaluate(self, inputs, ctx):
            self.calls += 1
            identity, end = f"{ctx.node_id}:{ctx.snapshot_id}", ctx.now + timedelta(days=1)
            meta = Metadata(identity, ctx.node_id, ctx.now, end)
            return NodeOutputs({"p": (PositionView({A: 0.2}, "weight", "account", meta),)})

    source = Weights()
    runtime = GraphRuntime(graph(source), MemoryServices())
    runtime.process(EventBatch(0, NOW, (Event("data"),)))
    kept = runtime.store.get("s", "p")
    # "20% of NAV in A" is the same position before and after a split.
    runtime.process(lifecycle_batch(1, "split", A, factor=0.5))
    assert runtime.store.get("s", "p") == kept and source.calls == 1
    # A delisting ends it.
    runtime.process(lifecycle_batch(2, "delisting", A))
    assert source.calls == 2 and runtime.store.get("s", "p") != kept


def test_identity_ledger_remembers_only_the_most_recent_identities():
    def view(identity, quantity):
        return PositionView(
            {A: quantity},
            "quantity",
            "account",
            Metadata(identity, "s", NOW, NOW + timedelta(minutes=1)),
        )

    store = ViewStore(identity_limit=2)
    for number in range(3):
        store.replace("s", NodeOutputs({"p": (view(f"v{number}", 1),)}))
    # v2 is still remembered: reusing it for different contents is rejected.
    with pytest.raises(ContractError, match="reused"):
        store.replace("s", NodeOutputs({"p": (view("v2", 5),)}))
    # v0 was the oldest of three and has been forgotten, keeping memory bounded.
    assert store.replace("s", NodeOutputs({"p": (view("v0", 5),)}))
    with pytest.raises(ContractError, match="at least one"):
        ViewStore(identity_limit=0)
