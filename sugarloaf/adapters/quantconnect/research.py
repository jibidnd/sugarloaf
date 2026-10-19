"""Causal QuantBook replay over QC history and native indicator resources.

History retrieval is not admission. Only one yielded batch is staged, and its
native inputs are applied when the runtime calls ``apply``. Historical receipt
times are assumed equal to observation end; actual vendor latency is unavailable.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from heapq import heappop, heappush
from typing import NamedTuple

from sugarloaf.core.errors import CapabilityError, ContractError, NotReady
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.core.primitives import InstrumentId, aware
from sugarloaf.market.contracts import (
    BarsRequirement,
    IndicatorRequirement,
    InstrumentRequirement,
    Observation,
    ResourceUpdate,
    Selection,
    UniverseRequirement,
)

from ..base import price_at_tick
from .lean import native_period
from .services import QuantConnectDataServices


class _Trade(NamedTuple):
    """A history bar reduced to what price_at_tick reads."""

    end: datetime
    available_at: datetime
    close: float
    fill_forward: bool


class QuantBookServices(QuantConnectDataServices):
    """Market-only historical provider with native bars and indicators.

    Args:
        qb (QuantBook): Native QuantBook instance.
        tickers (Mapping[InstrumentId, str] | None): Optional stable instrument-to-ticker mapping.
        api (LeanAPI | None): Optional injectable LeanAPI boundary.

    Each experiment requires fresh provider state. Universe, quote, account, and
    execution services are unsupported. Historical receipt equals observation end.
    """

    def __init__(self, qb, tickers=None, api=None):
        # Reuse native market resources without enabling account/execution hooks.
        # Replay owns its clock instead of borrowing QuantBook's current wall time.
        super().__init__(qb, api=api)
        self._replay_receipt = None
        # At most one yielded batch may wait for admission. Fresh providers keep
        # indicator state and input history independent across experiments.
        self._staged = None
        self._prepared = None
        self._actions = {}
        for instrument, ticker in (tickers or {}).items():
            self.register(instrument, ticker)

    def install(self, node_id: str, update: ResourceUpdate) -> None:
        """Install explicit instrument, bar, and indicator declarations.

        Raises:
            CapabilityError: A universe, quote, or portfolio read is requested.
        """
        # Reject unsupported dynamic membership and account reads explicitly;
        # research must not substitute today's universe or a synthetic portfolio.
        for requirement in update.requirements:
            if isinstance(requirement, UniverseRequirement) or getattr(
                getattr(requirement, "selection", None), "universe", None
            ):
                raise CapabilityError("QuantBook replay currently requires explicit instruments")
            if not isinstance(requirement, (BarsRequirement, IndicatorRequirement)):
                if not isinstance(requirement, InstrumentRequirement):
                    raise CapabilityError(
                        "QuantBook replay supports bars, indicators and instruments"
                    )
        super().install(node_id, update)

    def _receipt_time(self):
        # Historical vendor receipt times are unavailable. The replay clock
        # deliberately models receipt at observation end, as declared provenance.
        return self._replay_receipt or super()._receipt_time()

    def prepare(self, start: datetime, end: datetime) -> None:
        """Warm resources through start without decisions and retrieve typed lifecycle facts.

        Args:
            start: Aware silent warmup boundary and exclusive replay start.
            end: Aware inclusive replay end, strictly after start.

        Raises:
            ContractError: Dates are invalid or this provider has already been prepared.

        Retrieved future history and corporate actions remain unavailable until admitted.
        """
        aware(start)
        aware(end)
        if self._prepared is not None:
            raise ContractError("Use a fresh QuantBook provider for each experiment")
        if end <= start:
            raise ContractError("Research end must follow start")
        # Prepare is one-shot. Reusing this provider would retain learned native
        # indicator/resource state from the earlier experiment.
        self._prepared = (start, end)
        self._replay_receipt = start
        # Prefill through start silently. Decisions begin strictly after this
        # boundary, and warmup readiness is still checked per requirement.
        self._sync_resources(start)
        # Discard warmup callbacks as decision triggers; their admitted resource
        # values remain available to the first legitimate replay boundary.
        self.pending_events.clear()
        self._pending_data.clear()
        for instrument in self._requested_feeds():
            self._load_actions(instrument, start, end)

    def _load_actions(self, instrument, start, end):
        """Retrieve facts without exposing them before their effective boundary."""
        # Cache retrieval by exact interval without publishing future facts into
        # provider state. The merge below controls when lifecycle events appear.
        key = (instrument, start, end)
        if key in self._actions:
            return self._actions[key]
        symbol = self.symbols[instrument]
        security = self.algorithm.securities[symbol]
        actions = {}
        for action in self.api.corporate_actions(self.algorithm, symbol, start, end):
            at = self.api.to_utc(security, action.end_time)
            if not start < at <= end:
                continue
            if hasattr(action, "split_factor"):
                if "warning" in str(getattr(action, "type", "")).lower():
                    continue
                life = LifecycleEvent("split", instrument, at, factor=float(action.split_factor))
            elif hasattr(action, "distribution"):
                life = LifecycleEvent("dividend", instrument, at, amount=float(action.distribution))
            else:
                raise CapabilityError("Unsupported typed research corporate action")
            # Equal lifecycle facts collapse by immutable event identity; sort by
            # effective time later rather than relying on history retrieval order.
            actions[life] = Event("lifecycle", instrument, payload=life, end=at)
        self._actions[key] = tuple(sorted(actions.values(), key=lambda event: event.end))
        return self._actions[key]

    def _requested_feeds(self):
        feeds = {}
        for update in self.resources.values():
            for requirement in update.requirements:
                source = (
                    requirement.source
                    if isinstance(requirement, IndicatorRequirement)
                    else requirement
                )
                if not isinstance(source, BarsRequirement):
                    continue
                # Replay each security at the feed period a deployment subscribes
                # it at (planned in _sync_resources), so the replay ticks exactly
                # when the deployment would. A node that reads only daily bars
                # ticks at the close; a 10:30 decision needs a reader of intraday
                # data, or a schedule, whose minute feed then supplies the ticks.
                for instrument in source.selection.instruments:
                    period = self._feed_periods.get(instrument, native_period(source))
                    feeds[instrument] = replace(source, interval=period, lookback=1)
            # A session schedule still needs its calendar instrument's feed even
            # when no node is authorized to read that instrument's prices.
            for trigger in update.triggers:
                calendar = trigger.schedule.calendar if trigger.schedule else None
                if calendar is not None and calendar not in feeds:
                    feeds[calendar] = BarsRequirement(
                        Selection((calendar,)), timedelta(minutes=1), normalization="raw"
                    )
        return feeds

    def history_batches(self, start: datetime, end: datetime) -> Iterator[EventBatch]:
        """Merge native feeds and lifecycle facts by observation end.

        Args:
            start: Exact prepared start boundary.
            end: Exact prepared end boundary.

        Yields:
            One staged EventBatch with historical receipt assumed equal to event time.

        Raises:
            ContractError: Interval was not prepared, or the prior batch was not applied.

        Runtime must call apply before advancing the iterator. Resource changes rebuild
        future iterators without replaying admitted observations.
        """
        if self._prepared != (start, end):
            raise ContractError("Prepare this exact research interval before replay")
        feeds = None
        # Heap entries hold the next bar per feed plus sparse lifecycle facts.
        # This merges histories without admitting or materializing all future bars.
        pending_history = []
        iterators = {}
        admitted_through = start
        sequence = 0

        def stage_next_observation(index):
            instrument, source, iterator = iterators[index]
            security = self.algorithm.securities[self.symbols[instrument]]
            for bar in iterator:
                at = self.api.to_utc(security, bar.end_time)
                if admitted_through < at <= end:
                    # Kind 1 sorts bars after kind 0 lifecycle facts at an equal
                    # timestamp; apply resets native state before feeding the bars.
                    heappush(pending_history, (at, 1, index, 0, bar))
                    return

        while True:
            # Resource updates take effect at the runtime boundary. Rebuild only
            # historical iterators, never native indicator state or admitted values.
            requested = self._requested_feeds()
            if requested != feeds:
                feeds = requested
                pending_history = []
                iterators = {}
                for index, (instrument, source) in enumerate(sorted(feeds.items())):
                    symbol = self.symbols[instrument]
                    for ordinal, event in enumerate(
                        self._load_actions(instrument, admitted_through, end)
                    ):
                        heappush(pending_history, (event.end, 0, index, ordinal, event))
                    iterator = iter(
                        self.api.history_range(
                            self.algorithm, symbol, source, admitted_through, end
                        )
                    )
                    iterators[index] = (instrument, source, iterator)
                    stage_next_observation(index)
            if not pending_history:
                return
            # Group simultaneous inputs into one boundary so a node sees the
            # available cross-instrument slice rather than arbitrary iterator order.
            at = pending_history[0][0]
            incoming = []
            events = []
            while pending_history and pending_history[0][0] == at:
                _, kind, index, _, item = heappop(pending_history)
                if kind == 0:
                    events.append(item)
                    continue
                instrument, source, _ = iterators[index]
                incoming.append((instrument, source, item))
                stage_next_observation(index)
            if self._staged is not None:
                raise ContractError("Admit the previous research batch before requesting another")
            sequence += 1
            # Yielding advertises the boundary but does not update consolidators.
            # The runtime must call apply for this exact staged sequence first.
            self._staged = (sequence, tuple(incoming))
            if incoming:
                events.append(Event("data", end=at))
            yield EventBatch(sequence, at, tuple(events), at)
            # Resume only beyond the just-yielded boundary. Resource-driven feed
            # replacements must not replay inputs already admitted into indicators.
            admitted_through = at

    def apply(self, batch: EventBatch) -> list[Event]:
        """Admit the staged native inputs and effective lifecycle facts before node reads.

        Returns:
            Facts discovered during admission, such as completed consolidated bars.
        """
        if self._staged is None or self._staged[0] != batch.sequence:
            raise ContractError("Research batch has no staged native input")
        # Consolidator completion callbacks now stamp receipts using this replay
        # boundary rather than the QuantBook host's unrelated present-time clock.
        self._replay_receipt = batch.received_at
        lifecycle = any(isinstance(event.payload, LifecycleEvent) for event in batch.events)
        events: list[Event] = []
        if lifecycle:
            # Rebuild native state before feeding the first post-action observation.
            events = super().apply(batch)
        # Feed only this staged batch into matching native consolidators. Future
        # iterator values remain outside graph-visible windows and indicators.
        for instrument, _, bar in self._staged[1]:
            for key, consolidator in tuple(self.consolidators.items()):
                if key[1] != instrument:
                    continue
                # A replay feed carries bars of the security's feed period. The
                # native consolidators pass that interval through and build every
                # coarser declared one.
                consolidator.update(bar)
        # Flush any interval due at the cutoff, even without a sample exactly at
        # its close. The cutoff still prevents a later completed bar from leaking in.
        for key, consolidator in tuple(self.consolidators.items()):
            security = self.algorithm.securities[self.symbols[key[1]]]
            self.api.scan(consolidator, security, batch.cutoff)
        self._staged = None
        if lifecycle:
            # Base apply already performed lifecycle/resource work above. Admit
            # post-action completions without resetting those resources again.
            events.extend(self.pending_events)
            self.pending_events.clear()
            self._admit_pending_data()
            self._changed()
            return events
        # With no lifecycle reset, ordinary base admission handles all queued
        # completions and resource ownership in one pass.
        return super().apply(batch)

    def anchor_observation(
        self, instrument: InstrumentId, at: datetime, convention: str, as_of: datetime
    ) -> Observation | None:
        """The price at ``at`` from minute history, without altering strategy inputs.

        The price is read at the first clock tick this replay admitted at or after
        ``at``, by the cases in ``price_at_tick``: the instrument's bar at the tick,
        else its last trade earlier in the session, else its first trade after the
        tick in a session still open; no trade in the session is missing data.

        Args:
            instrument: Stable instrument identity.
            at: Aware requested time (an interval endpoint).
            convention: price_return uses split-adjusted history; total_return uses
                total-return history.
            as_of: Aware label-availability boundary.

        Returns:
            Observation for ``at``, available when its price became known, or None
            when the clock had not reached ``at``, the data is missing, or the price
            became known after as_of.

        Raises:
            CapabilityError: The return convention is unsupported.

        Native adjustment-vintage consistency requires separate platform validation.
        """
        # Labels query a separate history convention, not the strategy's input
        # window. This keeps outcome measurement from mutating forecast state.
        normalization = {"price_return": "split_adjusted", "total_return": "total_return"}.get(
            convention
        )
        if normalization is None:
            raise CapabilityError("Unsupported research outcome return convention")
        tick = self._first_tick(at)
        if tick is None or tick > as_of or instrument not in self.symbols:
            return None
        source = BarsRequirement(
            Selection((instrument,)), timedelta(minutes=1), normalization=normalization
        )
        symbol = self.symbols[instrument]
        security = self.algorithm.securities[symbol]
        opened, closes = self.api.session_bounds(security, tick, source.session == "extended")
        # History from the session open through the later of the tick and the
        # session close, never past the availability boundary.
        through = min(as_of, max(tick, closes))
        bars = [
            _Trade(
                self.api.to_utc(security, bar.end_time),
                self.api.to_utc(security, bar.end_time),  # replay receipt = event time
                float(bar.close),
                bool(getattr(bar, "is_fill_forward", False)),
            )
            for bar in self.api.history_range(self.algorithm, symbol, source, opened, through)
        ]
        try:
            value, known = price_at_tick(bars, tick, opened, closes, through)
        except NotReady:
            return None
        return Observation(at, known, value) if known <= as_of else None
