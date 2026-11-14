"""The node interface: declared inputs, view ports, and callbacks in the shape of an
alpha model.

A node is any object with the attributes and methods below; it need not inherit
from ``Node``. Its data declarations (``inputs``, ``indicators``, ``schedules``,
``warm_up``) are read once by the runner, which asks the adapter to set them up.
Its view ports (``ports``, ``outputs``) wire it into the graph. Its callbacks are
called by the runner as the platform's callbacks arrive, in topological order.

A node owns its state. It keeps what it needs between callbacks itself, as an
alpha model keeps per-symbol data; the platform keeps rolling windows and
indicators for it (``ctx.window``, ``ctx.indicator``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from sugarloaf.core.data import Slice
from sugarloaf.core.errors import ContractError, NotReady
from sugarloaf.core.inputs import Indicator, Input, Schedule, WarmUp
from sugarloaf.core.primitives import InstrumentId, frozen_map
from sugarloaf.core.views import Message

# What a callback returns: the node's complete current output, by output port.
# A port left out withdraws its previous views. None withdraws everything.
Views = Mapping[str, Sequence[Message]] | None


@dataclass(frozen=True)
class InputSpec:
    """Accepted View semantics and cardinality for one upstream port."""

    accepts: tuple[type[Message], ...]
    min_count: int = 1
    max_count: int | None = None
    statements: tuple[type, ...] = ()
    quantities: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    # Queries every statement must answer, such as ("mean",): a consumer that needs a
    # mean accepts an Estimate of the mean and a full Distribution alike.
    needs: tuple[str, ...] = ()

    def validate(self, views: tuple[Message, ...]) -> None:
        """Check actual input count, View classes, and declared statement semantics.

        Raises:
            NotReady: Fewer Views than min_count are available.
            ContractError: Count exceeds max_count or a View has unsupported semantics.
        """
        if len(views) < self.min_count:
            raise NotReady("Required input views are missing")
        if self.max_count is not None and len(views) > self.max_count:
            raise ContractError("Too many input views")
        for view in views:
            if not isinstance(view, self.accepts):
                raise ContractError(f"Unsupported View: {type(view).__name__}")
            statement = getattr(view, "statement", None)
            target = getattr(view, "target", None)
            if self.statements and not isinstance(statement, self.statements):
                raise ContractError("Unsupported statement representation")
            if self.quantities and getattr(target, "quantity", None) not in self.quantities:
                raise ContractError("Unsupported target quantity")
            if self.properties and getattr(statement, "property", None) not in self.properties:
                raise ContractError("Unsupported statement property")
            for query in self.needs:
                if statement is None or not statement.supports(query):
                    raise ContractError(f"Statement cannot answer the required query: {query}")


@dataclass(frozen=True)
class OutputSpec:
    """View classes and optional semantic guarantees offered by one output port."""

    emits: tuple[type[Message], ...]
    statements: tuple[type, ...] = ()
    quantities: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeInputs:
    """Current upstream Views, concatenated by destination port in edge order."""

    by_port: Mapping[str, tuple[Message, ...]]

    def __post_init__(self):
        object.__setattr__(
            self, "by_port", frozen_map({k: tuple(v) for k, v in self.by_port.items()})
        )

    def __getitem__(self, port: str) -> tuple[Message, ...]:
        return self.by_port.get(port, ())

    def get(self, port: str, default=()) -> tuple[Message, ...]:
        return self.by_port.get(port, default)


class Context(Protocol):
    """What a node may ask the platform for during a callback.

    The runner builds one per node per callback, over the adapter's mapping.
    """

    node_id: str
    time: datetime
    warming_up: bool

    def window(self, declaration, instrument: InstrumentId):
        """The rolling window of a ``Bars`` or ``Quotes`` declaration, newest first."""
        ...

    def indicator(self, name: str, instrument: InstrumentId):
        """The named indicator for an instrument: ``is_ready``, ``value``, ``values``."""
        ...

    def members(self, universe: str) -> tuple[InstrumentId, ...]:
        """Current members of a declared universe."""
        ...

    def history(self, request):
        """Past bars for a ``HistoryRequest``: {instrument: bars oldest first}."""
        ...

    def calendar(self, instrument: InstrumentId):
        """Session times: ``next_close``, ``next_open``, ``is_open``."""
        ...

    @property
    def portfolio(self):
        """Holdings, cash, NAV, marks and open orders, read-only."""
        ...


@runtime_checkable
class Node(Protocol):
    """A graph component with declared inputs and alpha-model callbacks.

    Attributes:
        inputs: ``Bars``, ``Quotes``, ``Custom`` and ``Universe`` declarations.
        indicators: ``Indicator`` declarations.
        schedules: ``Schedule`` declarations.
        warm_up: Optional ``WarmUp``.
        ports: Upstream view ports accepted, by name.
        outputs: View ports emitted, by name.
        config: Parameters recorded in run evidence.
    """

    inputs: Sequence[Input]
    indicators: Sequence[Indicator]
    schedules: Sequence[Schedule]
    warm_up: WarmUp | None
    ports: Mapping[str, InputSpec]
    outputs: Mapping[str, OutputSpec]
    config: Mapping[str, object]

    def on_securities_changed(self, ctx: Context, added, removed) -> None:
        """Universe membership changed: ``added`` and ``removed`` are instrument tuples."""
        ...

    def update(self, ctx: Context, data: Slice, views: NodeInputs) -> Views:
        """One slice of declared data plus the current upstream views; returns outputs."""
        ...

    def on_scheduled(self, ctx: Context, name: str, views: NodeInputs) -> Views:
        """A declared schedule fired; returns outputs or None to leave them unchanged."""
        ...


# Defaults for the optional attributes and callbacks, applied by the runner when a
# node omits them so that a minimal node declares only what it uses.
NODE_DEFAULTS = {
    "inputs": (),
    "indicators": (),
    "schedules": (),
    "warm_up": None,
    "ports": {},
    "outputs": {},
    "config": {},
}


def declared(node, name: str):
    """A node's declaration attribute, or its default when the node omits it."""
    return getattr(node, name, NODE_DEFAULTS[name])


def outputs_of(node, result: Views) -> dict[str, tuple[Message, ...]]:
    """Normalize a callback's return value to a port map and check the declared ports.

    Raises:
        ContractError: A port is undeclared or its views violate the port's spec.
    """
    if result is None:
        return {}
    if not isinstance(result, Mapping):
        raise ContractError("A node callback returns a mapping of port name to views")
    outputs = declared(node, "outputs")
    normalized = {}
    for port, views in result.items():
        if port not in outputs:
            raise ContractError(f"Undeclared output port: {port}")
        views = tuple(views)
        spec = outputs[port]
        InputSpec(
            spec.emits,
            min_count=0,
            statements=spec.statements,
            quantities=spec.quantities,
            properties=spec.properties,
        ).validate(views)
        normalized[port] = views
    return normalized
