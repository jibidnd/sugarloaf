"""Causal QuantBook replay over QC history and native indicator resources.

History retrieval is not admission. Only one yielded batch is staged, and its
native inputs are applied when the runtime calls ``apply``. Historical receipt
times are assumed equal to observation end; actual vendor latency is unavailable.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from heapq import heappop, heappush

from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.core.types import InstrumentId, aware
from sugarloaf.market.contracts import (
    BarsRequirement,
    IndicatorRequirement,
    InstrumentRequirement,
    Observation,
    ResourceUpdate,
    Selection,
    UniverseRequirement,
)

from .services import QuantConnectDataServices


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
            CapabilityError: A universe, quote, portfolio, or scalar-series read is requested.
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
                # Deployment subscribes minute upstream data even for daily nodes.
                # Replay the same clock ticks so a 10:30 decision cannot see the
                # day's later close merely because its node reads daily bars.
                source = replace(source, interval=timedelta(minutes=1), lookback=1)
                for instrument in source.selection.instruments:
                    feeds[instrument] = source
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
        """Merge native minute feeds and lifecycle facts by observation end.

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

    def apply(self, batch: EventBatch) -> None:
        """Admit the staged native inputs and effective lifecycle facts before node reads."""
        if self._staged is None or self._staged[0] != batch.sequence:
            raise ContractError("Research batch has no staged native input")
        # Consolidator completion callbacks now stamp receipts using this replay
        # boundary rather than the QuantBook host's unrelated present-time clock.
        self._replay_receipt = batch.received_at
        lifecycle = any(isinstance(event.payload, LifecycleEvent) for event in batch.events)
        if lifecycle:
            # Rebuild native state before feeding the first post-action observation.
            super().apply(batch)
        # Feed only this staged batch into matching native consolidators. Future
        # iterator values remain outside graph-visible windows and indicators.
        for instrument, _, bar in self._staged[1]:
            for key, consolidator in tuple(self.consolidators.items()):
                if key[1] != instrument:
                    continue
                # Replay feeds are always minute bars; native consolidators build
                # every declared interval, including exchange-session daily bars.
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
            self.additional_events.extend(self.pending_events)
            self.pending_events.clear()
            self._admit_pending_data()
            self._changed()
        else:
            # With no lifecycle reset, ordinary base admission handles all queued
            # completions and resource ownership in one pass.
            super().apply(batch)

    def anchor_observation(
        self, instrument: InstrumentId, at: datetime, convention: str, as_of: datetime
    ) -> Observation | None:
        """Query an exact minute-close label without altering strategy inputs.

        Args:
            instrument: Stable instrument identity.
            at: Exact aware endpoint timestamp.
            convention: price_return uses split-adjusted history; total_return uses
                total-return history.
            as_of: Aware label-availability boundary.

        Returns:
            Exact Observation, or None when missing or later than as_of.

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
        # Unavailable/future endpoints stay missing; do not backfill from the
        # nearest observed minute or infer a label before its availability boundary.
        if at > as_of or instrument not in self.symbols:
            return None
        source = BarsRequirement(
            Selection((instrument,)), timedelta(minutes=1), normalization=normalization
        )
        symbol = self.symbols[instrument]
        security = self.algorithm.securities[symbol]
        for bar in self.api.history_range(
            self.algorithm, symbol, source, at - timedelta(minutes=2), at
        ):
            observed = self.api.to_utc(security, bar.end_time)
            # Require the exact requested anchor. Native adjustment-vintage
            # correctness remains a separate platform-validation question.
            if observed == at:
                return Observation(observed, observed, float(bar.close))
        return None
