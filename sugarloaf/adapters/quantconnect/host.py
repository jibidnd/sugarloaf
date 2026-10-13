"""A thin serial event boundary around LEAN callbacks and neutral execution."""


from sugarloaf.core.errors import ContractError, NotReady, OrderRefused
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.execution.contracts import (
    ORDER_TYPES,
    TIMES_IN_FORCE,
    ExecutionPolicy,
    OrderUpdate,
)
from sugarloaf.execution.session import TradingSession
from sugarloaf.market.contracts import Capability

from .services import QuantConnectServices, utc


class QuantConnectGateway:
    """Translate execution intents and obtain fresh platform account contexts."""

    def __init__(self, services):
        self.services = services
        # Bind native IDs to neutral intents; the native order tag also carries
        # identity if synchronous feedback arrives before submit returns.
        self.orders = {}

    def capability(self, policy: ExecutionPolicy) -> Capability:
        """Accept regular-session day or gtc orders: market, market-on-open, market-on-close."""
        return Capability(
            policy.order_type in ORDER_TYPES
            and policy.time_in_force in TIMES_IN_FORCE
            and policy.session == "regular",
            "QC gateway supports regular-session day or gtc market, open and close orders",
        )

    def context(self):
        """Return fresh broker services at algorithm UTC time, independent of decision time."""
        services = self.services
        # Reconciliation reads current broker state even if the last decision
        # used an earlier observation cutoff. Provider versions bound cache life.
        now = utc(services.algorithm.utc_time, "UTC")
        batch = EventBatch(services.batch.sequence if services.batch else 0, now)
        return services.context(batch)

    def submit(self, intent):
        """Submit an intent-tagged native order, bind its platform ID, and invalidate Contexts."""
        services = self.services
        # Native submission can invoke callbacks before returning this ticket.
        # The host queues them while the trading decision is still active.
        ticket = services.api.submit(
            services.algorithm, services.symbols[intent.instrument], intent
        )
        services._changed()
        # LEAN refuses some orders before creating them (a market-on-close order
        # sent too late, for instance). Such a ticket has no order behind it and
        # no order event follows, so report the refusal now.
        refusal = services.api.refusal(ticket)
        if refusal is not None:
            raise OrderRefused("QC_ORDER_REFUSED: " + refusal)
        self.orders[str(ticket.order_id)] = intent.intent_id
        return str(ticket.order_id)

    def cancel(self, order_id: str) -> None:
        """Check the returned ticket's request response; acceptance is not closure."""
        try:
            ticket = self.services.algorithm.transactions.cancel_order(
                int(order_id), "Sugarloaf target invalidated"
            )
        finally:
            # An uncertain request can coincide with account changes; old Context
            # caches must not survive the request, even when it raises.
            self.services._changed()
        # Acceptance is not cancellation confirmation. Execution retains pending
        # exposure until terminal order feedback resolves the reservation.
        outcome = self._cancel_outcome(ticket)
        if outcome is not None and not outcome.supported:
            raise NotReady("QC_CANCEL_REJECTED: " + outcome.reason)

    def cancel_status(self, order_id: str) -> Capability | None:
        """Read later request rejection without inferring terminal order state."""
        ticket = self.services.algorithm.transactions.get_order_ticket(int(order_id))
        return self._cancel_outcome(ticket)

    @staticmethod
    def _cancel_outcome(ticket) -> Capability | None:
        if ticket is None:
            raise ContractError("QC cancellation has no platform order ticket")
        response = ticket.get_most_recent_order_response()
        if response is None:
            return None
        if response.is_error:
            return Capability(False, f"{response.error_code}: {response.error_message}")
        return Capability(True) if response.is_success else None


class QuantConnectHost:
    """Serialize native data decisions and cumulative order feedback.

    Args:
        algorithm (QCAlgorithm): Native QCAlgorithm forwarding its callbacks to this host.
        graph (CompiledGraph): Fresh position-producing compiled graph.
        symbols (Mapping[InstrumentId, Symbol] | None): Optional caller-owned
            instrument-to-native-symbol mapping.
        tickers (Mapping[InstrumentId, str] | None): Optional initial stable instrument-to-ticker
            mapping.
        constraints (PortfolioConstraints | None): Account execution-safety limits.
        policy (ExecutionPolicy | None): Supported order and staleness settings.
        api (LeanAPI | None): Optional injectable LeanAPI boundary.
        cost_rate (float): Proportional slippage used for estimates and native initialization.
        leverage (float): Native security leverage setting.

    Schedule and consolidation facts queue until OnData. Synchronous order
    feedback queues until the decision ends; all fills are applied before another
    reconciliation so holdings and pending reservations stay consistent.
    """

    def __init__(
        self,
        algorithm,
        graph,
        symbols=None,
        tickers=None,
        constraints=None,
        policy=None,
        api=None,
        cost_rate=0.0005,
        leverage=1.0,
    ):
        from sugarloaf.execution.engine import ExecutionEngine

        # Assemble native services -> gateway -> execution -> trading session.
        # The session owns graph evaluation; this host owns callback ordering.
        self.algorithm = algorithm
        self.services = QuantConnectServices(
            algorithm, symbols, constraints, api, cost_rate, leverage
        )
        for instrument, ticker in (tickers or {}).items():
            self.services.register(instrument, ticker)
        self.gateway = QuantConnectGateway(self.services)
        self.execution = ExecutionEngine(self.gateway, policy=policy or ExecutionPolicy())
        self.session = TradingSession(graph, self.services, self.execution)
        # Share the session's one runtime for clock checks and final reports.
        self.runtime = self.session.runtime
        self.sequence = 0
        # Serialize reconciliation: feedback that arrives while a decision or a
        # reconciliation is running asks for another pass instead of nesting one.
        self._busy = False
        self._reconcile_due = False
        # Callback-queued facts waiting for the next slice that carries data.
        self._held: list[Event] = []
        # The session installed node declarations. Create/warm their resources
        # without treating those historical samples as strategy decisions.
        self.services._sync_resources(utc(algorithm.utc_time, "UTC"))

    def schedule(self, key: str) -> None:
        """Queue an externally scheduled fact for the next data boundary."""
        self.services.pending_events.append(Event("scheduled", key=key))
        self.services._changed()

    def on_securities_changed(self, changes):
        """Bind added native symbols and queue a resource-change fact for the next data boundary."""
        # Native security admission is separate from universe selection. Bind
        # new symbols now; reconcile resource ownership at the apply boundary.
        for security in changes.added_securities:
            self.services.bind(security.symbol)
        self.services.pending_events.append(Event("resources"))
        self.services._changed()

    def _lifecycle(self, data, now):
        events = []
        # LEAN adjusts holdings for corporate actions. These neutral facts reset
        # affected data resources and invalidate old View authority; Sugarloaf
        # must not apply a second native holdings adjustment.
        for field, kind in (
            ("splits", "split"),
            ("dividends", "dividend"),
            ("symbol_changed_events", "symbol_change"),
            ("delistings", "delisting"),
        ):
            collection = getattr(data, field, {})
            for symbol, item in collection.items():
                instrument = self.services.bind(symbol)
                if kind == "split":
                    # Advance notice is not an effective split.
                    if "warning" in str(getattr(item, "type", "")).lower():
                        continue
                    life = LifecycleEvent(kind, instrument, now, factor=float(item.split_factor))
                elif kind == "dividend":
                    life = LifecycleEvent(kind, instrument, now, amount=float(item.distribution))
                elif kind == "symbol_change":
                    life = LifecycleEvent(
                        kind,
                        instrument,
                        now,
                        old_ticker=item.old_symbol,
                        new_ticker=item.new_symbol,
                    )
                else:
                    self.services.delisted.add(instrument)
                    life = LifecycleEvent(kind, instrument, now)
                events.append(Event("lifecycle", instrument, payload=life))
        return events

    def on_data(self, data):
        """Admit queued market/lifecycle facts, evaluate, then drain all order feedback.

        Args:
            data (Slice): Native LEAN Slice; completed data ends establish the graph clock.

        Algorithm UTC time supplies receipt time. Empty callbacks do not tick the
        graph. Warmup admits resources without decisions. Before the first data
        observation, lifecycle facts may cancel orders but cannot start a clock.
        """
        self._busy = True
        try:
            now = utc(self.algorithm.utc_time, "UTC")
            # Consolidators/universes/schedules queue facts rather than invoking
            # nodes. Collect those facts into this serial data boundary.
            # Facts held back by an earlier callback come first, in arrival order.
            queued = self._held + list(self.services.pending_events)
            self._held = []
            self.services.pending_events.clear()
            lifecycle = self._lifecycle(data, now)
            # Observation ends choose the cutoff; algorithm UTC time above is
            # receipt time. The two clocks describe different facts.
            ends = []
            for field in ("bars", "quote_bars"):
                for symbol, bar in getattr(data, field, {}).items():
                    security = self.algorithm.securities[symbol]
                    ends.append(self.services.api.to_utc(security, bar.end_time))
            ends.extend(event.end for event in queued if event.end is not None)
            if ends:
                events = queued + lifecycle
            else:
                # LEAN also calls OnData for a slice that carries no bars (only
                # corporate actions or other auxiliary data), typically before the
                # open. Nothing was observed, so the graph clock cannot advance
                # past yesterday's close. Lifecycle facts
                # are handled now, because they affect safety. Facts queued by
                # callbacks (a new universe selection, a schedule, a resource
                # change) are promised "the next data boundary" and stay queued.
                #
                # Delivering them here would evaluate their nodes with the clock
                # still at the previous close. A universe selected this morning is
                # then "from the future", the node is withdrawn as unready, and the
                # one trigger for that selection is spent. Seen natively: a node
                # triggered by universe selection missed two days out of five.
                #
                # They are held here rather than put back on the provider's queue:
                # provider admission drains that queue into the very batch being
                # processed, which would deliver them now after all.
                self._held = queued
                events = lifecycle
            prior = self.runtime.batch or self.services.batch
            # An empty callback does not create a graph decision opportunity.
            if not ends and not events:
                return
            if not ends and prior is None:
                # Safety feedback may cancel, but cannot start the graph clock.
                if any(isinstance(event.payload, LifecycleEvent) for event in events):
                    self.execution.invalidate()
                self.services.pending_events.extend(events)
                return
            # Events without data reuse the existing cutoff. Runtime/provider
            # admission handles late data without inventing a new observation.
            cutoff = max(ends) if ends else prior.cutoff
            self.sequence += 1
            if ends:
                events.append(Event("data", end=max(ends)))
            # Warmup completion does not bypass per-requirement readiness.
            if self.algorithm.is_warming_up:
                self.services.apply(EventBatch(self.sequence, cutoff, tuple(events), now))
            else:
                # Admit services, evaluate eligible nodes, then pass the final
                # position authority to execution within one session step.
                self.session.process(EventBatch(self.sequence, cutoff, tuple(events), now))
        finally:
            self._busy = False
        # Native fills may already be in holdings. Match all queued feedback to
        # local pending exposure before performing another reconciliation.
        self._drain_feedback()

    def on_order_event(self, event):
        """Pass feedback to execution at once, even when LEAN calls during submission."""
        # The provider version change invalidates any cached account Context.
        self.services._changed()
        # LEAN has already put this event's fill into holdings. The engine must
        # learn of it before it reads holdings again, so it is handed over now and
        # not held here. An idle engine applies it immediately; a reconciling one
        # queues it until the order it is submitting has its ID.
        self.execution.on_order_update(self._order_update(event))
        self._reconcile_due = True
        if not self._busy:
            self._drain_feedback()

    def _drain_feedback(self):
        if self._busy:
            return
        self._busy = True
        try:
            # Reconciliation can submit or cancel and so cause more feedback; repeat
            # until a pass ends with none before releasing the boundary.
            while self._reconcile_due:
                self._reconcile_due = False
                self.execution.reconcile()
        finally:
            self._busy = False

    def _order_update(self, event) -> OrderUpdate:
        """Read cumulative fills from the ticket, never sum event deltas twice."""
        ticket = self.algorithm.transactions.get_order_ticket(event.order_id)
        order = self.algorithm.transactions.get_order_by_id(event.order_id)
        # Tags bridge callbacks that beat submit()'s native-ID binding.
        identity = self.gateway.orders.get(str(event.order_id), order.tag)
        platform_status = str(event.status).split(".")[-1].lower().replace("_", "")
        statuses = {
            "partiallyfilled": "partial",
            "filled": "filled",
            "canceled": "canceled",
            "cancelpending": "pending_cancel",
            "invalid": "rejected",
            "submitted": "open",
            "new": "open",
            "updatesubmitted": "open",
        }
        if platform_status not in statuses:
            raise ContractError(f"Unsupported QC order status: {platform_status}")
        status = statuses[platform_status]
        # Use the cumulative ticket quantity, not an event delta: repeated or
        # delayed callbacks must not add the same fill twice.
        return OrderUpdate(
            str(event.order_id),
            identity,
            status,
            float(ticket.quantity_filled),
            utc(event.utc_time, "UTC"),
            event.message or "",
        )
