"""Deterministic DAG compilation with structural and declared semantic checks."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sugarloaf.core.errors import ContractError
from sugarloaf.core.node import Node
from sugarloaf.core.views import PositionView, View


@dataclass(frozen=True)
class Edge:
    """A directed connection from one output port to one input port."""
    source_node: str
    source_port: str
    destination_node: str
    destination_port: str


@dataclass(frozen=True)
class CompiledGraph:
    """Frozen wiring and deterministic order; node instances still own run state."""

    # Wiring is read-only, but node objects may keep state such as monthly gates.
    # Construct a new graph for each independent run rather than copying this handle.
    nodes: Mapping[str, Node]
    edges: tuple[Edge, ...]
    topological_order: tuple[str, ...]
    output: tuple[str, str]

    def require_positions(self) -> None:
        """Require the designated output to declare PositionView exclusively.

        Raises:
            ContractError: The output declaration is not exactly (PositionView,).

        TradingSession separately checks actual output cardinality at runtime.
        """
        # Predictive graphs may end in forecasts. TradingSession explicitly invokes
        # this additional check because only position targets authorize execution.
        node, port = self.output
        if self.nodes[node].outputs[port].emits != (PositionView,):
            raise ContractError("Trading output must emit only PositionView")

    def consumers(self, node_id: str) -> tuple[str, ...]:
        """Return unique direct consumer IDs in sorted order."""
        # Runtime activation propagates one step through these consumers whenever a
        # producer's effective output changes or expires.
        return tuple(sorted({e.destination_node for e in self.edges if e.source_node == node_id}))

    def incoming(self, node_id: str) -> tuple[Edge, ...]:
        """Return incoming edges in registration order."""
        return tuple(e for e in self.edges if e.destination_node == node_id)


class Graph:
    """Build a typed DAG with one designated output, then compile before running."""

    def __init__(self):
        # The builder owns declarations until compile freezes their wiring. Register
        # all nodes/edges and designate one terminal port before creating a runtime.
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._output: tuple[str, str] | None = None

    def add_node(self, node_id: str, node: Node) -> "Graph":
        """Register a node instance and return this builder.

        Args:
            node_id: Nonempty unique identity used in View provenance.
            node: Fresh stateful instance for this graph run.

        Raises:
            ContractError: The identity is empty or already registered.
        """
        # The node ID becomes its message source/provenance. Reusing a mutable node
        # across independent runs would share decision state even with different IDs.
        if not node_id or node_id in self._nodes:
            raise ContractError(f"Duplicate or empty node ID: {node_id}")
        self._nodes[node_id] = node
        return self

    def connect(
        self, source_node: str, source_port: str, destination_node: str, destination_port: str
    ) -> "Graph":
        """Register a directed port connection and return this builder.

        Node existence, port existence, and compatibility are checked by compile.

        Raises:
            ContractError: This exact edge is already registered.
        """
        # Connections carry View messages between named ports, not Python method calls.
        # The runtime later invokes each activated node in topological order.
        edge = Edge(source_node, source_port, destination_node, destination_port)
        if edge in self._edges:
            raise ContractError("Duplicate graph edge")
        self._edges.append(edge)
        return self

    def set_output(self, node_id: str, port: str) -> "Graph":
        """Designate the terminal node and output port, validated during compilation."""
        self._output = node_id, port
        return self

    def compile(self) -> CompiledGraph:
        """Validate the DAG and freeze its wiring without copying node state.

        Returns:
            Compiled graph with deterministic topological order and one terminal output.

        Raises:
            ContractError: Wiring, declared semantics, cardinality, or terminal output
                is invalid, or the graph contains a cycle.

        Actual View cardinality and data readiness are checked during evaluation.
        """
        # Validate the declared interface before any provider subscriptions/evaluation.
        # This catches incompatible wiring; actual data/readiness remains a runtime concern.
        if self._output is None:
            raise ContractError("A graph requires one designated terminal output")
        for edge in self._edges:
            if edge.source_node not in self._nodes or edge.destination_node not in self._nodes:
                raise ContractError("Edge refers to an unknown node")
            source = self._nodes[edge.source_node].outputs.get(edge.source_port)
            target = self._nodes[edge.destination_node].inputs.get(edge.destination_port)
            if source is None or target is None:
                raise ContractError("Edge refers to an unknown port")
            if (
                not source.emits
                or not target.accepts
                or any(
                    not any(issubclass(t, accepted) for accepted in target.accepts)
                    for t in source.emits
                )
            ):
                raise ContractError("Incompatible concrete View classes on edge")
            # Concrete View classes alone do not guarantee matching predictive semantics.
            # Declared statement forms/quantities/properties must also be compatible.
            for name in ("statements", "quantities", "properties"):
                offered, accepted = getattr(source, name), getattr(target, name)
                if offered and accepted and not set(offered) <= set(accepted):
                    raise ContractError(f"Incompatible declared {name} on edge")
        output_node, output_port = self._output
        if output_node not in self._nodes or output_port not in self._nodes[output_node].outputs:
            raise ContractError("Unknown graph output")
        emits = self._nodes[output_node].outputs[output_port].emits
        if not emits or any(not issubclass(kind, View) for kind in emits):
            raise ContractError("Graph output must emit View types")
        if any(e.source_node == output_node for e in self._edges):
            raise ContractError("Graph output node must be terminal")
        for node_id, node in self._nodes.items():
            for port, spec in node.inputs.items():
                if spec.min_count < 0 or (
                    spec.max_count is not None and spec.max_count < spec.min_count
                ):
                    raise ContractError("Invalid port cardinality")
                if spec.min_count and not any(
                    e.destination_node == node_id and e.destination_port == port
                    for e in self._edges
                ):
                    raise ContractError(f"Required input is disconnected: {node_id}.{port}")
        # Kahn-style topological ordering: repeatedly choose nodes whose producers
        # are already scheduled. Sorting makes independent branches deterministic.
        pending = set(self._nodes)
        order: list[str] = []
        while pending:
            ready = sorted(
                n
                for n in pending
                if not any(
                    e.destination_node == n and e.source_node in pending for e in self._edges
                )
            )
            if not ready:
                raise ContractError("Graph contains a cycle")
            order.extend(ready)
            pending.difference_update(ready)
        # Freeze a copy of the builder's wiring while preserving these fresh node
        # instances; compile does not clone or reset their internal state.
        return CompiledGraph(
            MappingProxyType(dict(self._nodes)), tuple(self._edges), tuple(order), self._output
        )
