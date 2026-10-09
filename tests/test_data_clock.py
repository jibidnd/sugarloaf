"""Event time, admission and scheduled decisions remain separate concerns."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryServices
from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import ReturnView
from sugarloaf.market.contracts import Schedule, Trigger
from sugarloaf.runtime import GraphRuntime

START = datetime(2025, 1, 6, 10, 29, tzinfo=UTC)
A = InstrumentId("A")


class Forecast:
    inputs = {}
    outputs = {"forecast": OutputSpec((ReturnView,))}
    requirements = ()
    triggers = (Trigger("data"),)

    def __init__(self):
        self.observed = []

    def evaluate(self, inputs, ctx):
        self.observed.append((ctx.now, ctx.available_at, ctx.snapshot_id))
        end = ctx.now + timedelta(days=1)
        return NodeOutputs(
            {
                "forecast": (
                    ReturnView.create(
                        A,
                        Interval(ctx.now, end),
                        0.01,
                        0.03,
                        Metadata(f"f:{ctx.snapshot_id}", ctx.node_id, ctx.now, end),
                    ),
                )
            }
        )


def make_runtime(node, **kwargs):
    graph = Graph().add_node("forecast", node).set_output("forecast", "forecast").compile()
    return GraphRuntime(graph, MemoryServices(), **kwargs)


def test_late_and_equal_time_arrivals_have_distinct_admission_provenance():
    node = Forecast()
    emissions = []
    runtime = make_runtime(node, on_output=lambda *args: emissions.append(args))
    first = EventBatch(1, START, (Event("data", end=START),), START + timedelta(seconds=2))
    runtime.process(first)
    # A slower source arrives next. The graph stays at START, but sees new information.
    late = START - timedelta(minutes=1)
    second = EventBatch(2, late, (Event("data", end=late),), START + timedelta(seconds=3))
    runtime.process(second)
    runtime.process(second)
    runtime.process(EventBatch(3, START, (Event("data", end=START),), START + timedelta(seconds=4)))
    assert [row[0] for row in node.observed] == [START] * 3
    assert [row[2] for row in node.observed] == [1, 2, 3]
    assert [emission[2].received_at for emission in emissions] == [
        START + timedelta(seconds=n) for n in (2, 3, 4)
    ]
    with pytest.raises(ContractError, match="receipt"):
        runtime.process(EventBatch(4, START, received_at=START))


def test_schedule_crossing_is_coalesced_and_not_repeated_at_equal_time():
    node = Forecast()
    node.triggers = (Trigger("scheduled", schedule=Schedule(at=time(10, 30))),)
    runtime = make_runtime(node)
    runtime.process(EventBatch(1, START, (Event("data"),)))
    assert not node.observed
    now = START + timedelta(minutes=3)
    runtime.process(EventBatch(2, now, (Event("data"),)))
    runtime.process(EventBatch(3, now, (Event("data"),)))
    assert len(node.observed) == 1
    # Multiple crossed daily schedules yield one current decision, not past trades.
    runtime.process(EventBatch(4, now + timedelta(days=3), (Event("data"),)))
    assert len(node.observed) == 2


def test_predictive_graph_cannot_be_attached_to_execution():
    runtime = make_runtime(Forecast())
    with pytest.raises(ContractError, match="PositionView"):
        runtime.graph.require_positions()


def test_late_lifecycle_event_cannot_lower_adjustment_basis_watermark():
    class OldBasis(Forecast):
        def evaluate(self, inputs, ctx):
            output = super().evaluate(inputs, ctx)
            view = output.by_port["forecast"][0]
            return NodeOutputs(
                {
                    "forecast": (
                        replace(
                            view, metadata=replace(view.metadata, cutoff=START - timedelta(hours=1))
                        ),
                    )
                }
            )

    runtime = make_runtime(OldBasis())
    split = LifecycleEvent("split", A, START, factor=0.5)
    runtime.process(EventBatch(1, START, (Event("lifecycle", A, payload=split),)))
    assert not runtime.store.get("forecast", "forecast")
    # A second, older split arrives late. Its effective time (START - 2h) is
    # before the forecast's information cutoff (START - 1h), so if it lowered the
    # watermark the stale forecast below would wrongly be accepted.
    earlier = LifecycleEvent("split", A, START - timedelta(hours=2), factor=2)
    runtime.process(
        EventBatch(
            2,
            START,
            (Event("lifecycle", A, payload=earlier), Event("data")),
            received_at=START + timedelta(seconds=1),
        )
    )
    assert not runtime.store.get("forecast", "forecast")
    assert (
        "basis" in runtime.diagnostics[-1].message or "lifecycle" in runtime.diagnostics[-1].message
    )
