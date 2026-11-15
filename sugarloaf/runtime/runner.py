"""The graph runner: declarations in, callbacks dispatched, views carried, target out.

``GraphRunner`` reads every node's declarations, asks the adapter to set them up
(``start``), and then handles the adapter's callbacks: ``on_data`` for a slice of
records, ``on_securities_changed`` for universe membership, ``on_scheduled`` for a
declared schedule, ``on_order_event`` for order feedback, ``finish`` at the end.
Each callback runs the nodes it concerns in topological order, carries their views
along the graph edges and, when the terminal output is a position target that
changed, checks it against the portfolio constraints and hands it to the adapter.
In a forecast run (a graph whose output port emits views) it archives every
predictive view instead.
"""

from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sugarloaf.adapters.protocol import Adapter, Arrival
from sugarloaf.core.data import Slice
from sugarloaf.core.errors import ContractError, NotReady, NumericalError
from sugarloaf.core.graph import CompiledGraph
from sugarloaf.core.inputs import (
    Bars,
    Custom,
    HistoryRequest,
    Indicator,
    Quotes,
    Schedule,
    Universe,
    WarmUp,
)
from sugarloaf.core.node import NodeInputs, declared, outputs_of
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.views import Message, PositionView
from sugarloaf.execution.contracts import (
    ExecutionPolicy,
    OrderRecord,
    OrderUpdate,
    desired_quantity,
)
from sugarloaf.portfolio.constraints import PortfolioConstraints, violations
from sugarloaf.research.archive import ForecastArchive

# Ledger marker for an identity whose views were withdrawn by a lifecycle fact.
_RETIRED = object()


@dataclass(frozen=True)
class Diagnostic:
    """A node failure code and message tied to the platform time it happened at."""

    code: str
    node: str
    time: datetime
    message: str


@dataclass
class NodeContext:
    """What a node may ask for during one callback; see ``core.node.Context``."""

    runner: "GraphRunner"
    node_id: str
    time: datetime
    warming_up: bool = False
    _indicators: Mapping[str, Indicator] = field(default_factory=dict)

    @property
    def sequence(self) -> int:
        """The runner's step count: with the node id, a run-unique identity for a view."""
        return self.runner.steps

    def window(self, declaration, instrument: InstrumentId):
        return self.runner.adapter.window(declaration, instrument)

    def indicator(self, name: str, instrument: InstrumentId):
        try:
            declaration = self._indicators[name]
        except KeyError:
            raise ContractError(f"{self.node_id} declares no indicator named {name}") from None
        return self.runner.adapter.indicator(declaration, instrument)

    def members(self, universe: str) -> tuple[InstrumentId, ...]:
        return self.runner.adapter.members(universe)

    def history(self, request: HistoryRequest):
        return self.runner.adapter.history(request)

    def calendar(self, instrument: InstrumentId):
        return self.runner.adapter.calendar(instrument)

    @property
    def portfolio(self):
        return self.runner.adapter.portfolio()


class GraphRunner:
    """Run a compiled graph over an adapter.

    Args:
        graph: Compiled wiring and fresh node instances for this run.
        adapter: The platform mapping.
        constraints: Limits a position target must satisfy before execution.
        policy: How a target becomes orders.
        identity_limit: How many recent view identities the ledger remembers for
            the one-identity-one-content check.
    """

    def __init__(
        self,
        graph: CompiledGraph,
        adapter: Adapter,
        *,
        constraints: PortfolioConstraints | None = None,
        policy: ExecutionPolicy | None = None,
        identity_limit: int = 100_000,
    ):
        self.graph, self.adapter = graph, adapter
        self.constraints = constraints or PortfolioConstraints()
        self.policy = policy or ExecutionPolicy()
        self.trading = graph.emits_positions()
        self.archive = ForecastArchive()
        self.diagnostics: deque[Diagnostic] = deque(maxlen=2000)
        self.diagnostic_count = 0
        self.orders: list[OrderRecord] = []
        self.updates: list[OrderUpdate] = []
        self.steps = 0
        self.last_target: PositionView | None = None
        self._outputs: dict[str, dict[str, tuple[Message, ...]]] = {
            node: {} for node in graph.nodes
        }
        self._identities: OrderedDict = OrderedDict()
        self._identity_limit = identity_limit
        self._started = False
        # Declarations by node, gathered once.
        self._bars: dict[str, tuple[Bars | Quotes, ...]] = {}
        self._customs: dict[str, tuple[Custom, ...]] = {}
        self._universes: dict[str, Universe] = {}
        self._universe_readers: dict[str, set[str]] = {}
        self._schedules: dict[str, str] = {}
        self._indicators: dict[str, dict[str, Indicator]] = {}
        self._consumers = {node: graph.consumers(node) for node in graph.nodes}
        self._incoming = {node: graph.incoming(node) for node in graph.nodes}
        self._gather()

    # ----------------------------------------------------------------- setup

    def _gather(self) -> None:
        for node_id, node in self.graph.nodes.items():
            feeds, customs = [], []
            for item in declared(node, "inputs"):
                if isinstance(item, (Bars, Quotes)):
                    feeds.append(item)
                elif isinstance(item, Custom):
                    customs.append(item)
                elif isinstance(item, Universe):
                    previous = self._universes.get(item.name)
                    if previous is not None and previous != item:
                        raise ContractError(f"Conflicting definitions of universe {item.name}")
                    self._universes[item.name] = item
                    self._universe_readers.setdefault(item.name, set()).add(node_id)
                else:
                    raise ContractError(f"{node_id} declares an unknown input: {item!r}")
            names = [f.name for f in feeds if f.name is not None]
            if len(set(names)) != len(names):
                raise ContractError(f"{node_id} names two feed declarations alike")
            self._bars[node_id] = tuple(feeds)
            self._customs[node_id] = tuple(customs)
            indicators = {}
            for item in declared(node, "indicators"):
                if not isinstance(item, Indicator) or item.name in indicators:
                    raise ContractError(f"{node_id} declares an invalid or repeated indicator")
                indicators[item.name] = item
            self._indicators[node_id] = indicators
            for item in declared(node, "schedules"):
                if not isinstance(item, Schedule) or item.name in self._schedules:
                    raise ContractError("Schedule names must be unique across the graph")
                self._schedules[item.name] = node_id
            for item in (*feeds, *customs, *indicators.values()):
                universe = item.selection.universe
                if universe is not None:
                    self._universe_readers.setdefault(universe, set()).add(node_id)
        for universe in self._universe_readers:
            if universe not in self._universes:
                raise ContractError(f"No node declares the universe {universe}")

    def start(self) -> None:
        """Set up every declaration on the adapter, once, before the clock starts."""
        if self._started:
            raise ContractError("The runner has already started")
        self._started = True
        # Shared feeds: two nodes asking for the same feed and instruments share
        # one subscription; the adapter keeps the largest window asked for.
        feeds: dict[tuple, dict] = {}
        for node_id, declarations in self._bars.items():
            for item in declarations:
                if item.selection.universe is None:
                    feeds.setdefault(item.key, {}).setdefault("explicit", []).append(item)
                else:
                    feeds.setdefault(item.key, {}).setdefault(item.selection.universe, []).append(
                        item
                    )
        # Finest feeds first: a platform gives a security one subscription, and a coarser
        # declaration on the same security is consolidated from it.
        order = {"minute": 0, "hour": 1, "daily": 2}
        for key in sorted(feeds, key=lambda k: (order[k[1]], k[2] or timedelta(0))):
            groups = feeds[key]
            for group, items in groups.items():
                if group != "explicit":
                    continue
                instruments = tuple(
                    dict.fromkeys(i for item in items for i in item.selection.instruments)
                )
                widest = max(items, key=lambda item: item.window)
                self.adapter.subscribe(widest, instruments)
        for name, definition in self._universes.items():
            member_feeds = tuple(
                max(items, key=lambda item: item.window)
                for groups in feeds.values()
                for group, items in groups.items()
                if group == name
            )
            self.adapter.add_universe(definition, member_feeds)
        seen = set()
        for declarations in self._customs.values():
            for item in declarations:
                if item not in seen:
                    seen.add(item)
                    self.adapter.add_custom(item)
        for node_id, indicators in self._indicators.items():
            for item in indicators.values():
                self.adapter.add_indicator(item, item.selection.instruments)
        for name, node_id in self._schedules.items():
            schedule = next(
                s for s in declared(self.graph.nodes[node_id], "schedules") if s.name == name
            )
            self.adapter.schedule(schedule)
        warm = [
            declared(node, "warm_up")
            for node in self.graph.nodes.values()
            if declared(node, "warm_up") is not None
        ]
        if warm:
            self.adapter.warm_up(_longest_warm_up(warm))

    # -------------------------------------------------------------- callbacks

    def on_data(self, arrival: Arrival) -> None:
        """Dispatch one slice: the nodes it touches run, then whatever depends on them."""
        self.steps += 1
        changed: set[str] = set()
        for node_id in self.graph.topological_order:
            data = self._slice_for(node_id, arrival)
            upstream = any(producer in changed for producer in self._producers(node_id))
            if data.empty and not upstream:
                continue
            if self._run(node_id, arrival.time, arrival.warming_up, "update", data):
                changed.add(node_id)
        if not arrival.warming_up:
            self._terminal(arrival.time)

    def on_securities_changed(
        self,
        universe: str,
        added: tuple[InstrumentId, ...],
        removed: tuple[InstrumentId, ...],
        warming_up: bool = False,
    ) -> None:
        """Forward a membership change to every node that declares or selects the universe.

        ``warming_up`` is the platform's warm-up state, as ``Arrival.warming_up`` is for
        ``on_data``; the nodes' ``ctx.warming_up`` carries it.
        """
        time = self.adapter.time
        for node_id in self.graph.topological_order:
            if node_id not in self._universe_readers.get(universe, ()):
                continue
            node = self.graph.nodes[node_id]
            callback = getattr(node, "on_securities_changed", None)
            if callback is None:
                continue
            ctx = self._context(node_id, time, warming_up)
            callback(ctx, tuple(added), tuple(removed))

    def on_scheduled(self, name: str, warming_up: bool = False) -> None:
        """Run the node owning the named schedule, then whatever depends on it.

        ``warming_up`` is the platform's warm-up state, as for ``on_data``: the nodes see it
        in ``ctx.warming_up``, their outputs are not kept, and no target reaches
        ``adapter.execute``.
        """
        self.steps += 1
        time = self.adapter.time
        owner = self._schedules.get(name)
        if owner is None:
            raise ContractError(f"No node declares the schedule {name}")
        changed: set[str] = set()
        for node_id in self.graph.topological_order:
            if node_id == owner:
                ran = self._run(node_id, time, warming_up, "on_scheduled", name)
            elif any(producer in changed for producer in self._producers(node_id)):
                ran = self._run(node_id, time, warming_up, "update", Slice(time))
            else:
                continue
            if ran:
                changed.add(node_id)
        if not warming_up:
            self._terminal(time)

    def on_order_event(self, update: OrderUpdate) -> None:
        """Keep order feedback for the run's evidence."""
        self.updates.append(update)
        del self.updates[:-10_000]

    def finish(self) -> dict:
        """A summary of the run, for the run summary the adapter reports."""
        return {
            "steps": self.steps,
            "diagnostic_count": self.diagnostic_count,
            "diagnostics": [vars(d) for d in list(self.diagnostics)[-20:]],
            "orders": len(self.orders),
            "forecasts": len(self.archive),
            "last_target": None
            if self.last_target is None
            else self.last_target.metadata.view_id,
        }

    # ---------------------------------------------------------------- reads

    def outputs(self, node_id: str, port: str | None = None):
        """A node's current views, by port or for one port."""
        current = self._outputs[node_id]
        return current.get(port, ()) if port is not None else dict(current)

    def terminal(self) -> tuple[Message, ...]:
        node, port = self.graph.output
        return self._outputs[node].get(port, ())

    # ------------------------------------------------------------- internals

    def _producers(self, node_id: str) -> tuple[str, ...]:
        return tuple(edge.source_node for edge in self._incoming[node_id])

    def _context(self, node_id: str, time: datetime, warming_up: bool) -> NodeContext:
        return NodeContext(self, node_id, time, warming_up, self._indicators[node_id])

    def _instruments(self, node_id: str) -> set[InstrumentId]:
        """Every instrument the node's declarations select right now."""
        result: set[InstrumentId] = set()
        for item in (*self._bars[node_id], *self._customs[node_id],
                     *self._indicators[node_id].values()):
            result.update(self._resolve(item.selection))
        return result

    def _resolve(self, selection) -> tuple[InstrumentId, ...]:
        if selection.universe is not None:
            return self.adapter.members(selection.universe)
        return selection.instruments

    def _slice_for(self, node_id: str, arrival: Arrival) -> Slice:
        """The part of an arrival this node declared."""
        bars: dict = {}
        quotes: dict = {}
        named: dict = {}
        for item in self._bars[node_id]:
            delivered = (arrival.bars if isinstance(item, Bars) else arrival.quotes).get(item.key)
            if not delivered:
                continue
            wanted = self._resolve(item.selection)
            picked = {i: delivered[i] for i in wanted if i in delivered}
            if not picked:
                continue
            if item.name is not None:
                named.setdefault(item.name, {}).update(picked)
            elif isinstance(item, Bars):
                bars.update(picked)
            else:
                quotes.update(picked)
        custom: dict = {}
        for item in self._customs[node_id]:
            records = arrival.custom.get(item.kind, ())
            if not records:
                continue
            if item.selection.universe is not None or item.selection.instruments:
                wanted = set(self._resolve(item.selection))
                records = tuple(r for r in records if r.instrument in wanted)
            if records:
                custom.setdefault(item.kind, [])
                custom[item.kind].extend(records)
        lifecycle = {}
        if arrival.dividends or arrival.splits or arrival.delistings or arrival.symbol_changes:
            mine = self._instruments(node_id)
            for name in ("dividends", "splits", "delistings", "symbol_changes"):
                lifecycle[name] = tuple(
                    item for item in getattr(arrival, name) if item.instrument in mine
                )
        return Slice(arrival.time, bars, quotes, named, custom, **lifecycle)

    def _inputs(self, node_id: str, time: datetime) -> NodeInputs:
        ports = {port: () for port in declared(self.graph.nodes[node_id], "ports")}
        for edge in self._incoming[node_id]:
            views = self._outputs[edge.source_node].get(edge.source_port, ())
            # Expired views are not evidence; they drop out before a consumer sees them.
            ports[edge.destination_port] += tuple(
                v for v in views if v.metadata.expires_at > time
            )
        return NodeInputs(ports)

    def _run(self, node_id: str, time: datetime, warming_up: bool, callback: str, argument):
        """Run one callback and store its outputs. Returns whether the outputs changed."""
        node = self.graph.nodes[node_id]
        method = getattr(node, callback, None)
        if method is None:
            return False
        ctx = self._context(node_id, time, warming_up)
        inputs = self._inputs(node_id, time)
        try:
            for port, spec in declared(node, "ports").items():
                spec.validate(inputs.by_port[port])
            result = method(ctx, argument, inputs)
            views = outputs_of(node, result)
            # A view restated after its expiry is no longer evidence: it drops out, as it
            # would from a consumer's inputs, rather than ending the run.
            views = {
                port: tuple(v for v in port_views if v.metadata.expires_at > time)
                for port, port_views in views.items()
            }
            self._check(node_id, views, time)
        except (NotReady, NumericalError) as error:
            if not warming_up:
                # Warm-up replays history to fill state; "not ready" there is expected.
                self.diagnostic_count += 1
                self.diagnostics.append(Diagnostic(error.code, node_id, time, str(error)))
            views = {}
        if warming_up:
            # Warm-up fills windows and node state; nothing it produces is kept.
            return False
        previous = self._outputs[node_id]
        for port_views in views.values():
            for view in port_views:
                self._remember(view.metadata.view_id, view)
        self._outputs[node_id] = views
        if not self.trading:
            for port, port_views in views.items():
                self.archive.record(node_id, port, port_views, time)
        return previous != views

    def _check(self, node_id: str, views: Mapping, time: datetime) -> None:
        seen = set()
        for port_views in views.values():
            for view in port_views:
                metadata = view.metadata
                if metadata.view_id in seen:
                    raise ContractError("Duplicate View identity in one emission")
                seen.add(metadata.view_id)
                if metadata.source != node_id or metadata.cutoff > time:
                    raise ContractError("Output source or cutoff is invalid")
                known = self._identities.get(metadata.view_id)
                if known is not None and known is not _RETIRED and known != view:
                    raise ContractError("View identity reused with different contents")

    def _remember(self, identity: str, value) -> None:
        self._identities[identity] = value
        self._identities.move_to_end(identity)
        while len(self._identities) > self._identity_limit:
            self._identities.popitem(last=False)

    def _terminal(self, time: datetime) -> None:
        if not self.trading:
            return
        views = self.terminal()
        if len(views) != 1 or not isinstance(views[0], PositionView):
            return
        target = views[0]
        if self.last_target is not None and self.last_target == target:
            return
        if target.metadata.expires_at <= time:
            return
        failures = self._violations(target)
        if failures:
            self.diagnostic_count += 1
            self.diagnostics.append(
                Diagnostic("TARGET_REJECTED", self.graph.output[0], time, ", ".join(failures))
            )
            return
        self.last_target = target
        records = self.adapter.execute(target, self.policy)
        self.orders.extend(records)
        del self.orders[:-10_000]

    def _violations(self, target: PositionView) -> tuple[str, ...]:
        """Check the target's effective account weights against the constraints."""
        portfolio = self.adapter.portfolio()
        if portfolio.nav <= 0:
            return ("account NAV is not positive",)
        current = portfolio.weights()
        weights = {}
        for instrument, amount in target.positions.items():
            if target.units == "weight":
                weights[instrument] = amount
                continue
            price = portfolio.prices.get(instrument)
            if price is None or price <= 0:
                # No mark: the adapter will skip it; it adds no exposure to check.
                continue
            weights[instrument] = desired_quantity(amount, "quantity", price, portfolio.nav)[1] * (
                price / portfolio.nav
            )
        effective = {**current, **weights}
        return violations(effective, self.constraints, current)


def _longest_warm_up(items: list[WarmUp]) -> WarmUp:
    """The warm-up covering every node's request: by span when any gives a span."""
    spans = [w.span for w in items if w.span is not None]
    counts = [w for w in items if w.count is not None]
    if spans and not counts:
        return WarmUp(span=max(spans))
    if counts and not spans:
        order = {"minute": 0, "hour": 1, "daily": 2}
        # The coarsest resolution with the largest count of its bars.
        coarsest = max(counts, key=lambda w: (order[w.resolution], w.count))
        return coarsest
    # Mixed requests: a span can express both.
    from sugarloaf.core.inputs import RESOLUTION_PERIODS

    longest = max(
        [*spans, *(w.count * RESOLUTION_PERIODS[w.resolution] * 1.6 for w in counts)]
    )
    return WarmUp(span=longest)
