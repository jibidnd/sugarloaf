"""Batch events carry neutral payloads, never platform objects."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, aware, finite, frozen_map


def _immutable_payload(value):
    # Events cross provider/runtime boundaries. Recursively freeze neutral values
    # so later native callback mutation cannot change an already admitted arrival.
    if isinstance(value, (str, int, float, bool, datetime, timedelta, type(None))):
        return value
    if isinstance(value, Mapping):
        return frozen_map({key: _immutable_payload(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_immutable_payload(item) for item in value)
    if is_dataclass(value) and value.__dataclass_params__.frozen:
        return replace(
            value,
            **{
                field.name: _immutable_payload(getattr(value, field.name))
                for field in fields(value)
                if field.init
            },
        )
    raise ContractError("Events require neutral immutable payloads")


@dataclass(frozen=True)
class LifecycleEvent:
    """An instrument lifecycle fact with an aware effective time.

    A split requires its positive price factor. Neutral support for other event
    kinds does not imply that a particular adapter supplies them.
    """
    # This is a fact about an instrument, not an instruction to place an order.
    # The host/session/runtime decide which old resources and targets it invalidates.
    kind: str
    instrument: InstrumentId
    effective_at: datetime
    factor: float | None = None
    amount: float | None = None
    old_ticker: str | None = None
    new_ticker: str | None = None

    def __post_init__(self):
        aware(self.effective_at)
        finite(*(value for value in (self.factor, self.amount) if value is not None))
        if self.kind not in {
            "split",
            "dividend",
            "symbol_change",
            "delisting",
            "expiry",
            "exercise",
            "assignment",
        }:
            raise ContractError("Unknown instrument lifecycle event")
        if self.kind == "split" and (self.factor is None or self.factor <= 0):
            raise ContractError("Split requires positive price factor")


@dataclass(frozen=True)
class DataReset:
    """A provider's report that it discarded and rebuilt data some nodes had declared.

    Attributes:
        instrument: The instrument whose retained data was rebuilt.
        nodes: IDs of the nodes that declared that data.
        effective_at: Aware time from which the rebuilt data applies.

    Only the provider knows whether a corporate action changed the meaning of
    prices it already holds. That depends on the vendor and the price convention:
    one vendor restates adjusted history on an ex-dividend date, another supplies
    a series that was continuous all along. So the provider decides, rebuilds
    what it must, and reports it with this fact. The runtime withdraws the named
    nodes' outputs, and everything derived from them, and evaluates them again.

    The core therefore holds no rule about dividends or price conventions. A
    provider that rebuilt nothing reports nothing, and nothing is withdrawn.
    """

    instrument: InstrumentId
    nodes: tuple[str, ...]
    effective_at: datetime

    def __post_init__(self):
        aware(self.effective_at)
        object.__setattr__(self, "nodes", tuple(self.nodes))
        if not self.nodes:
            raise ContractError("A data reset must name the nodes whose data was rebuilt")


@dataclass(frozen=True)
class Event:
    """A triggerable fact with optional matching filters and immutable payload.

    Attributes:
        kind: Event category matched by node triggers.
        instrument: Optional stable instrument identity.
        interval: Optional data interval filter.
        key: Optional named event filter.
        payload: Neutral immutable data; platform objects are rejected.
        end: Observation boundary used to advance the graph clock, when supplied.
    """
    # kind/key/instrument/interval are trigger filters; end can advance observation
    # time. payload carries neutral facts rather than provider SDK objects.
    kind: str
    instrument: InstrumentId | None = None
    interval: timedelta | None = None
    key: str | None = None
    payload: object = None
    end: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, "payload", _immutable_payload(self.payload))
        if self.end is not None:
            aware(self.end)


@dataclass(frozen=True)
class EventBatch:
    """An admitted input batch, with separate observation and receipt times.

    ``cutoff`` is the input's event-time boundary; the runtime clamps it to the
    graph clock. ``received_at`` is when this batch's information became available.
    Equal event times may belong to distinct, ordered batches.
    """

    # Sequence identifies an arrival; cutoff describes observation time and
    # received_at describes knowledge time. Late data can have an older cutoff.
    sequence: int
    cutoff: datetime
    events: tuple[Event, ...] = ()
    received_at: datetime | None = None

    def __post_init__(self):
        # Default receipt to cutoff only when the caller supplies no distinct arrival
        # time. Reject future observations whose data could not yet have been received.
        aware(self.cutoff)
        received = self.cutoff if self.received_at is None else self.received_at
        aware(received)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "events", tuple(self.events))
        if self.sequence < 0:
            raise ContractError("Batch sequence must be nonnegative")
        if self.cutoff > received or any(
            event.end is not None and event.end > received for event in self.events
        ):
            raise ContractError("Input observations cannot follow their receipt boundary")
        if any(
            isinstance(event.payload, LifecycleEvent) and event.payload.effective_at > received
            for event in self.events
        ):
            raise ContractError("Lifecycle event cannot follow its availability boundary")
