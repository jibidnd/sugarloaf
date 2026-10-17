from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sugarloaf.adapters.memory.services import MemoryServices
from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import InputSpec, NodeInputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.services import AccountSnapshot
from sugarloaf.core.views import (
    Distribution,
    DistributionView,
    Estimate,
    Ordering,
    PositionView,
    Probability,
    ProbabilityView,
    RankingView,
    RelativeReturnView,
    ReturnEvent,
    ReturnView,
    Score,
    ScoreView,
    Target,
    return_target,
)
from sugarloaf.market.contracts import Instrument, ResourceUpdate, UniverseMembership
from sugarloaf.nodes.positions import PositionNetter

NOW = datetime(2024, 1, 2, tzinfo=timezone.utc)
A, B = InstrumentId("A"), InstrumentId("B")
INTERVAL = Interval(NOW, NOW + timedelta(days=10))
META = Metadata("v", "source", NOW, INTERVAL.end)


def test_all_concrete_views_keep_structured_semantics():
    target = return_target(A, INTERVAL)
    estimate = ReturnView.create(A, INTERVAL, 0.03, 0.02, META)
    relative = RelativeReturnView.create(A, B, INTERVAL, 0.02, 0.01, META)
    ranking = RankingView.create(((A, B),), INTERVAL, 0.01, META)
    probability = ProbabilityView(target, Probability((ReturnEvent(lower=0),), (0.8,)), META)
    score = ScoreView(target, Score(2, "calibration-v1"), META)
    assert estimate.statement == Estimate(0.03, 0.02)
    assert relative.target.components[1].subject == B
    assert ranking.statement.groups == ((A, B),)
    assert probability.statement.probabilities == (0.8,)
    assert score.statement.calibration_id == "calibration-v1"
    with pytest.raises(ContractError):
        ReturnView(target, Estimate(0.1, 0.1, property="variance"), META)
    with pytest.raises(ContractError):
        Estimate(0.1, 0)
    with pytest.raises(ContractError):
        Ordering(((A,), (A,)), 0.1)
    with pytest.raises(ContractError):
        Score(float("nan"), "model")


def test_probability_partition_validation_does_not_normalize_independent_events():
    assert (
        sum(Probability((ReturnEvent(lower=0), ReturnEvent(lower=0.1)), (0.8, 0.6)).probabilities)
        > 1
    )
    with pytest.raises(ContractError, match="partition"):
        Probability((ReturnEvent(lower=0), ReturnEvent(lower=0.1)), (0.4, 0.6), exhaustive=True)
    Probability((ReturnEvent(upper=0), ReturnEvent(lower=0)), (0.4, 0.6), exhaustive=True)


def test_joint_distribution_retains_dependence_and_units():
    class JointReturns(Distribution):
        support = ((-1, float("inf")), (-1, float("inf")))
        units = "decimal"
        dimension = 2
        correlation = ((1, 0.7), (0.7, 1))

    joint = JointReturns()
    view = DistributionView(Target((A, B), "return", INTERVAL), joint, META)
    assert view.statement.correlation[0][1] == 0.7
    with pytest.raises(ContractError, match="units"):
        DistributionView(Target((A, B), "return", INTERVAL, units="USD"), joint, META)
    with pytest.raises(ContractError, match="dimension"):
        DistributionView(Target(A, "return", INTERVAL), joint, META)


def test_position_maps_are_immutable_and_finite():
    values = {A: 0.2}
    view = PositionView(values, "weight", "branch", META)
    values[A] = 0.9
    assert view.positions[A] == 0.2
    with pytest.raises(TypeError):
        view.positions[A] = 0.5
    with pytest.raises(ContractError):
        PositionView({A: float("inf")}, "weight", "branch", META)


def test_batch_payloads_cannot_be_changed_by_the_provider_after_construction():
    payload = {"members": [A]}
    batch = EventBatch(1, NOW, (Event("selection", payload=payload),))
    payload["members"].append(B)
    assert batch.events[0].payload["members"] == (A,)
    with pytest.raises(TypeError):
        batch.events[0].payload["members"] = ()


def test_instrument_expiry_and_universe_cutoff_require_aware_times():
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ContractError, match="timezone aware"):
        Instrument(A, "A", expiry=naive)
    with pytest.raises(ContractError, match="timezone aware"):
        UniverseMembership("assets", (A,), naive)
    members = [A]
    membership = UniverseMembership("assets", members, NOW)
    members.append(B)
    assert membership.members == (A,)


class Node:
    requirements = triggers = ()

    def __init__(self, inputs=None, outputs=None):
        self.inputs = inputs or {}
        self.outputs = outputs or {"out": OutputSpec((PositionView,))}


def test_graph_checks_cycles_types_semantics_and_terminal():
    graph = Graph().add_node("a", Node()).add_node("b", Node({"in": InputSpec((ReturnView,))}))
    graph.connect("a", "out", "b", "in").set_output("b", "out")
    with pytest.raises(ContractError, match="concrete"):
        graph.compile()
    graph = Graph().add_node("a", Node()).add_node("b", Node())
    graph.set_output("a", "out")
    assert graph.compile().topological_order == ("a", "b")
    a = Node({"in": InputSpec((PositionView,))})
    b = Node({"in": InputSpec((PositionView,))})
    graph = Graph().add_node("a", a).add_node("b", b).add_node("terminal", Node())
    graph.connect("a", "out", "b", "in").connect("b", "out", "a", "in")
    graph.set_output("terminal", "out")
    with pytest.raises(ContractError, match="cycle"):
        graph.compile()


def test_netter_shared_instrument_budgets_and_explicit_exits():
    services = MemoryServices(
        (Instrument(A, "A"),), AccountSnapshot({}, 10000, {"USD": 10000}, {A: 100})
    )
    netter = PositionNetter({"left": 0.4, "right": 0.4})
    services.install("net", ResourceUpdate(netter.requirements, ()))
    services.apply(EventBatch(1, NOW))
    left = PositionView({A: 20}, "quantity", "left", META)
    right = PositionView({A: 0.3}, "weight", "right", replace(META, view_id="right"))
    result = netter.evaluate(
        NodeInputs({"left": (left,), "right": (right,)}), services.context(node_id="net")
    ).by_port["positions"][0]
    assert result.positions[A] == 0.5
    assert result.attribution["left"][A] == 0.2
    omitted = PositionView({}, "weight", "left", META)
    with pytest.raises(ContractError, match="explicit zero"):
        netter.evaluate(
            NodeInputs({"left": (omitted,), "right": (right,)}), services.context(node_id="net")
        )
    zero = PositionView({A: 0}, "weight", "left", META)
    result = netter.evaluate(
        NodeInputs({"left": (zero,), "right": (right,)}), services.context(node_id="net")
    ).by_port["positions"][0]
    assert result.positions[A] == 0.3


def test_bar_permits_nonpositive_prices_but_not_inconsistent_ranges():
    from sugarloaf.market.contracts import Bar

    start = datetime(2020, 4, 20, 13, 30, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    # Front-month crude settled below zero on this date; back-adjusted continuous
    # futures can be negative too. Such a bar is valid data, not a contract error.
    bar = Bar(InstrumentId("CL"), start, end, end, 18.0, 18.5, -40.0, -37.6, 1000)
    assert bar.low < 0 and bar.close < 0
    # The range itself must still be coherent: the low cannot exceed the close.
    with pytest.raises(ContractError, match="OHLCV"):
        Bar(InstrumentId("CL"), start, end, end, 18.0, 18.5, -30.0, -37.6, 1000)
