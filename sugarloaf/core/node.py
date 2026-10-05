"""Graph ports declare concrete types and semantic capabilities."""

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from sugarloaf.core.errors import ContractError
from sugarloaf.core.services import Context
from sugarloaf.core.types import frozen_map
from sugarloaf.core.views import View
from sugarloaf.market.contracts import Requirement, ResourceUpdate, Trigger


@dataclass(frozen=True)
class InputSpec:
    """Accepted View semantics and cardinality for one input port."""

    # Cardinality is per destination port after connected producer outputs have
    # been concatenated. Semantic filters add requirements beyond Python View type.
    accepts: tuple[type[View], ...]
    min_count: int = 1
    max_count: int | None = None
    statements: tuple[type, ...] = ()
    quantities: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()

    def validate(self, views: tuple[View, ...]) -> None:
        """Check actual input count, View classes, and declared statement semantics.

        Raises:
            NotReady: Fewer Views than min_count are available.
            ContractError: Count exceeds max_count or a View has unsupported semantics.
        """
        # Missing required evidence is NotReady, allowing output withdrawal. Receiving
        # more Views or unsupported semantics is a contract violation, not missing data.
        if len(views) < self.min_count:
            from sugarloaf.core.errors import NotReady

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


@dataclass(frozen=True)
class OutputSpec:
    """View classes and optional semantic guarantees offered by one output port."""

    # Declare capabilities for graph compilation; the runtime still checks each
    # actual emitted View against these guarantees.
    emits: tuple[type[View], ...]
    statements: tuple[type, ...] = ()
    quantities: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeInputs:
    """Current upstream Views, concatenated by destination port in edge order."""

    # These are the current upstream snapshots for one evaluation. Retaining old
    # inputs in node state does not keep their authority alive after expiry.
    by_port: Mapping[str, tuple[View, ...]]

    def __post_init__(self):
        object.__setattr__(
            self, "by_port", frozen_map({k: tuple(v) for k, v in self.by_port.items()})
        )


@dataclass(frozen=True)
class NodeOutputs:
    """Complete output replacement, plus optional declarations for the next batch.

    Omitted ports withdraw their previous Views. Returning an existing View keeps
    its identity and timestamps; it does not renew the forecast or its expiry.
    ``resources=None`` retains declarations; ResourceUpdate replaces them next batch.
    """

    # Return every port you intend to keep active. An omitted port withdraws old
    # messages; a ResourceUpdate changes permissions only at the next batch boundary.
    by_port: Mapping[str, tuple[View, ...]]
    resources: ResourceUpdate | None = None

    def __post_init__(self):
        object.__setattr__(
            self, "by_port", frozen_map({k: tuple(v) for k, v in self.by_port.items()})
        )


class Node(Protocol):
    """Strategy component evaluated at most once per admitted graph batch.

    Requirements authorize service reads; triggers request evaluation. Changes to
    upstream Views can also activate the node. Keep mutable state per run and use
    Context only during evaluation: adapters invalidate it after the snapshot ends.
    """

    # This is a structural protocol: user classes need not inherit from Node.
    # They supply declarations and evaluate(inputs, ctx); no provider SDK is required.
    inputs: Mapping[str, InputSpec]
    outputs: Mapping[str, OutputSpec]
    requirements: Sequence[Requirement]
    triggers: Sequence[Trigger]

    def evaluate(self, inputs: NodeInputs, ctx: Context) -> NodeOutputs:
        """Evaluate one admitted snapshot and return the complete output replacement.

        Args:
            inputs: Current upstream Views grouped by input port.
            ctx: Declared services and graph/receipt times; valid only during evaluation.

        Returns:
            New outputs for all ports and optional resource declarations for next batch.

        Raises:
            NotReady: Required evidence is unavailable; runtime withdraws the output.
            NumericalError: Calculation failed; runtime withdraws and records a diagnostic.
        """
        ...
