"""Shared immutable primitives. All timestamps must be timezone aware."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Mapping, TypeVar

from sugarloaf.core.errors import ContractError

K = TypeVar("K")
V = TypeVar("V")


def frozen_map(value: Mapping[K, V]) -> Mapping[K, V]:
    """Copy a mapping into a read-only proxy; nested values are not copied."""
    # Copy the outer mapping before freezing it so the caller cannot later mutate
    # the published keys/values through its original dictionary.
    return MappingProxyType(dict(value))


def aware(value: datetime) -> None:
    """Validate that a datetime has a usable timezone.

    Raises:
        ContractError: The timestamp is naive or has no UTC offset.
    """
    # Never guess a timezone: an apparently ordered naive timestamp could refer
    # to a different market/session and violate causal admission.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("Timestamps must be timezone aware")


def finite(*values: float) -> None:
    """Validate that every supplied number is finite.

    Raises:
        ContractError: A value is NaN or infinite.
    """
    # Validate at the contract boundary instead of allowing NaN comparisons to
    # quietly skip risk limits or poison later model fitting.
    if not all(isfinite(x) for x in values):
        raise ContractError("Values must be finite")


@dataclass(frozen=True, order=True)
class InstrumentId:
    """Stable identity, independent of current ticker or a platform Symbol."""

    # Use this neutral identity in graph messages and account mappings. A ticker
    # name may change while the adapter preserves the same native security identity.
    value: str

    def __post_init__(self):
        if not self.value:
            raise ContractError("Instrument identity cannot be empty")


@dataclass(frozen=True)
class Interval:
    """An aware start/end pair with a strictly positive calendar duration.

    Raises:
        ContractError: Either timestamp is naive, or end does not follow start.
    """
    start: datetime
    end: datetime

    def __post_init__(self):
        aware(self.start)
        aware(self.end)
        if self.end <= self.start:
            raise ContractError("Interval end must follow start")

    @property
    def years(self) -> float:
        """Return the calendar duration in ACT/365 years."""
        # Model annualization uses actual elapsed calendar seconds divided by 365 days.
        # This is distinct from counting a fixed number of trading sessions.
        return (self.end - self.start).total_seconds() / (365 * 86400)


@dataclass(frozen=True)
class Metadata:
    """Immutable identity and provenance for one View.

    Attributes:
        view_id: Run-unique identity that cannot later describe different contents.
        source: Registered ID of the emitting node.
        cutoff: Logical graph time of the information used.
        expires_at: Exclusive validity boundary, strictly after cutoff.
        parents: Unique upstream View identities, excluding this View itself.
        model_version: Producer-defined version for research comparisons.

    Receipt time is recorded separately by the research archive.
    """

    # The forecast horizon belongs to its Target; expires_at is the lifetime of
    # this message's authority. They need not describe the same interval.
    view_id: str
    source: str
    cutoff: datetime
    expires_at: datetime
    parents: tuple[str, ...] = ()
    model_version: str = "1"

    def __post_init__(self):
        # Freeze provenance and reject duplicate parents or a self reference. The runtime
        # separately verifies that source matches the registered emitting node.
        aware(self.cutoff)
        aware(self.expires_at)
        object.__setattr__(self, "parents", tuple(self.parents))
        if not self.view_id or not self.source or self.expires_at <= self.cutoff:
            raise ContractError("View identity, source and positive lifetime are required")
        if len(set(self.parents)) != len(self.parents) or self.view_id in self.parents:
            raise ContractError("Invalid parent identities")
