"""Provider-neutral declaration checks, snapshots, readiness and ownership."""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sugarloaf.core.errors import CapabilityError, ContractError, NotReady, SnapshotError
from sugarloaf.core.events import DataReset, Event, EventBatch, LifecycleEvent
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.services import Context
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    ETFConstituentsUniverse,
    FundamentalsRequirement,
    FundamentalUniverse,
    IndicatorRequirement,
    InstrumentRequirement,
    LiquidityUniverse,
    PortfolioRequirement,
    QuotesRequirement,
    ResourceUpdate,
    Schedule,
    ShortAvailabilityRequirement,
    UniverseMembership,
    UniverseRequirement,
)
from sugarloaf.portfolio.constraints import PortfolioConstraints


@dataclass(frozen=True)
class SelectionRow:
    """Available selection inputs for one stable instrument identity."""

    instrument: InstrumentId
    price: float | None = None
    volume: float | None = None
    dollar_volume: float | None = None
    weight: float | None = None
    # Whether the provider has company fundamental data for the security; None
    # when the provider cannot say.
    has_fundamentals: bool | None = None


def select(definition, rows, at=None):
    """Select point-in-time members with stable identity breaking ranking ties.

    Args:
        definition (LiquidityUniverse | ETFConstituentsUniverse | FundamentalUniverse):
            Liquidity, historical ETF constituent or fundamental selection rule.
        rows (Sequence[SelectionRow | FundamentalRecord]): Available rows at the selection
            boundary; FundamentalRecord rows for a FundamentalUniverse.
        at (datetime | None): The selection time, required by a FundamentalUniverse.

    Returns:
        (tuple[InstrumentId, ...]): Ordered selected identities.

    Raises:
        NotReady: Required selection inputs are missing.
        CapabilityError: The universe definition is unsupported.
    """
    # Consume only supplied selection rows; never fetch future constituents or
    # substitute guessed values for missing ranking inputs.
    if isinstance(definition, LiquidityUniverse):
        if any(r.price is None or r.volume is None or r.dollar_volume is None for r in rows):
            raise NotReady("Liquidity selection fields missing")
        if definition.require_fundamentals and any(r.has_fundamentals is None for r in rows):
            # An unknown is not a no: selecting on a guess would change membership.
            raise NotReady("Liquidity selection needs to know which securities have fundamentals")
        rows = [
            r
            for r in rows
            if all(isfinite(v) for v in (r.price, r.volume, r.dollar_volume))
            and r.price >= definition.min_price
            and r.volume >= definition.min_volume
            and (r.has_fundamentals or not definition.require_fundamentals)
        ]
        # Stable identity makes ties independent of provider iteration order.
        rows.sort(key=lambda r: (-r.dollar_volume, r.instrument.value))
        return _first_distinct(rows, definition.count)
    if isinstance(definition, ETFConstituentsUniverse):
        if definition.missing_weight == "error" and any(r.weight is None for r in rows):
            raise NotReady("ETF constituent weight missing")
        rows = [
            r
            for r in rows
            if (r.weight is not None and isfinite(r.weight))
            or definition.missing_weight == "include"
        ]
        if definition.min_weight is not None:
            rows = [r for r in rows if r.weight is not None and r.weight >= definition.min_weight]
        rows.sort(key=lambda r: (-(r.weight if r.weight is not None else -1), r.instrument.value))
        return _first_distinct(rows, definition.count)
    if isinstance(definition, FundamentalUniverse):
        if at is None:
            raise ContractError("Fundamental selection needs its selection time")
        # The rule sees exactly the fields its definition declares, whatever the provider
        # happened to read.
        rows = tuple(r.project(definition.fields) for r in rows)
        available = {r.instrument for r in rows}
        picked = list(definition.rule(at, rows))
        # A ticker or native symbol would match nothing and leave a silently empty universe.
        if any(not isinstance(i, InstrumentId) for i in picked):
            raise ContractError("A fundamental rule must return InstrumentId values")
        # The rule chooses; selection keeps only securities the provider listed, once
        # each, in the rule's order.
        return tuple(dict.fromkeys(i for i in picked if i in available))
    raise CapabilityError("Unsupported universe definition")


def _first_distinct(rows, count):
    """The first `count` distinct identities of ranked rows.

    A provider can list one security twice in a day's selection data. A member is
    selected once, by its best-ranked row, and a repeat does not use up a place.
    """
    members = dict.fromkeys(row.instrument for row in rows)
    return tuple(members)[:count]


def price_at_tick(
    bars: Sequence[Bar],
    tick: datetime,
    session_open: datetime,
    session_close: datetime,
    through: datetime,
) -> tuple[float, datetime]:
    """The instrument's price at a clock tick, and when that price became known.

    The clock moves only when data arrives, from any instrument, so a price asked
    for at some time is the price at the first tick at or after it (``tick``). The
    instrument may have no trade at that tick; how that is treated depends on its
    session, the one that opened at ``session_open`` (the latest open at or before
    the tick) and closes at ``session_close``:

    1. A bar of the instrument ends at the tick: its close.
    2. No trade at the tick, but the instrument traded earlier in that session: its
       last trade stands, so the close of its last bar before the tick, known at
       the tick. This also covers a tick after the close and before the next open.
    3. No trade yet in a session still open at the tick (just after the open, or a
       quiet overnight session): the previous session's price would carry the move
       between sessions into the wrong interval, so the price is the instrument's
       first trade in the session, known when that bar arrives.
    4. No trade in the session at all: the data is missing.

    Args:
        bars: The instrument's bars, earliest first. Bars the provider repeated
            because nothing traded (``fill_forward``) are not trades and are ignored.
        tick: The first clock tick at or after the requested time.
        session_open: Open of the session containing the tick, or the latest one
            before it.
        session_close: That session's close.
        through: Time up to which bars have been received.

    Returns:
        (price, time the price became known).

    Raises:
        NotReady: No trade in the session (case 4), or none yet (case 3).
    """
    last_end = max(tick, session_close)
    trades = [b for b in bars if not b.fill_forward and session_open < b.end <= last_end]
    before = [b for b in trades if b.end <= tick]
    if before:
        last = before[-1]
        # Case 1 when it ends exactly at the tick; case 2 otherwise.
        return last.close, last.available_at if last.end == tick else max(tick, last.available_at)
    if tick <= session_close:
        after = [b for b in trades if b.end > tick]
        if after:
            return after[0].close, after[0].available_at
        if through < session_close:
            raise NotReady("No trade yet in the session")
    raise NotReady("No trade in the session")


def resource_key(req, instrument):
    """Identify backing data by kind/instrument/settings, excluding lookback and freshness."""
    # Compatible readers share storage. Their individual lookbacks/freshness
    # limits are enforced after reading, not encoded into this backing identity.
    return (
        type(req).__name__,
        instrument,
        req.interval,
        req.session,
        getattr(req, "normalization", None),
        req.fill_forward,
    )


class ProviderServices:
    """Reusable declaration, snapshot, readiness, and ownership machinery.

    Args:
        constraints (PortfolioConstraints | None): Portfolio safety limits exposed through Context.
        account_services (bool): Expose account, valuation, and cost hooks when true.

    Subclasses supply backing reads and native capabilities. Requirements authorize
    reads; triggers control activation. Provider mutations invalidate old Contexts.
    """

    def __init__(self, constraints=None, account_services=False):
        self.instruments = {}
        self.constraints = constraints or PortfolioConstraints()
        self.account_services = account_services
        # Complete declarations grant node read permissions; triggers separately
        # determine when eligible nodes evaluate.
        self.resources = {}
        self.readiness = {}
        self.memberships = {}
        self.selection_inputs = {}
        self.owners = {}
        # Contexts bind to this version. Mutations invalidate their lazy reads
        # rather than allowing cached state to cross an admission boundary.
        self.version = 0
        self.batch = None
        # Admitted clock ticks (batch cutoffs), earliest first. A price asked for
        # at some time is read at the first tick at or after it.
        self.ticks = []
        self.read_counts = {}
        self._snapshot_cache = {}
        # Exposure/residence retention is separate from graph read permission.
        # Keeping a resource alive does not authorize an undeclared node access.
        self.retained_resources = set()
        self._residence_until = {}

    def _changed(self):
        # Invalidate existing Contexts and all shared memoized backing reads.
        # Providers call this for account/data/resource mutations as well as batches.
        self.version += 1
        self._snapshot_cache.clear()

    def install(self, node_id: str, update: ResourceUpdate) -> None:
        """Replace complete declarations, rebuild ownership, and invalidate prior Contexts.

        Args:
            node_id: Registered owner of these resources.
            update: Complete replacement of requirements and triggers.

        Raises:
            CapabilityError: A declaration or requested service is unsupported.
            ContractError: Owners give conflicting definitions for a shared universe.
        """
        # Validate before publishing a complete declaration replacement. Concrete
        # providers can reject capabilities beyond this shared supported vocabulary.
        supported = (
            BarsRequirement,
            IndicatorRequirement,
            QuotesRequirement,
            FundamentalsRequirement,
            ShortAvailabilityRequirement,
            PortfolioRequirement,
            InstrumentRequirement,
            UniverseRequirement,
        )
        for req in update.requirements:
            if not isinstance(req, supported):
                raise CapabilityError(f"Unsupported requirement: {type(req).__name__}")
            if isinstance(req, IndicatorRequirement):
                self._validate_indicator(req)
            if isinstance(req, PortfolioRequirement) and not self.account_services:
                raise CapabilityError("Provider has no portfolio service")
            declared = (
                req.fields
                if isinstance(req, FundamentalsRequirement)
                else req.definition.fields
                if isinstance(req, UniverseRequirement)
                and isinstance(req.definition, FundamentalUniverse)
                else ()
            )
            unknown = [name for name in declared if not self._supports_fundamental_field(name)]
            if unknown:
                # Fail when the graph is installed, not at the first read hours later.
                raise CapabilityError(f"Unsupported fundamental fields: {', '.join(unknown)}")
            if isinstance(req, UniverseRequirement):
                # A shared universe name must mean the same rule for every owner.
                # Conflicting definitions must not depend on installation order.
                previous = [
                    r.definition
                    for other, u in self.resources.items()
                    if other != node_id
                    for r in u.requirements
                    if isinstance(r, UniverseRequirement)
                    and r.definition.name == req.definition.name
                ]
                if previous and any(p != req.definition for p in previous):
                    raise ContractError("Conflicting definitions for a shared universe")
        # Replacement is complete, not a union. Removed requirements lose their
        # permission and backing ownership when this map is rebuilt.
        self.resources[node_id] = update
        self.readiness[node_id] = "requested"
        self._rebuild_owners()
        self._changed()

    def _rebuild_owners(self):
        # Expand dynamic membership at the current admitted boundary and collect
        # every node sharing each compatible backing source.
        owners = {}
        residence = {
            r.definition.name: r.definition.minimum_residence
            for u in self.resources.values()
            for r in u.requirements
            if isinstance(r, UniverseRequirement)
        }
        for node, update in self.resources.items():
            for req in update.requirements:
                # An indicator owns its underlying input source as well.
                if isinstance(req, IndicatorRequirement):
                    req = req.source
                if isinstance(req, (BarsRequirement, QuotesRequirement)):
                    members = (
                        self.memberships.get(req.selection.universe)
                        if req.selection.universe
                        else None
                    )
                    ids = members.members if members else req.selection.instruments
                    for instrument in ids:
                        key = resource_key(req, instrument)
                        owners.setdefault(key, set()).add(node)
                        # Start residence on first ownership at a known boundary;
                        # repeated refreshes do not restart the minimum period.
                        if key not in self.owners and self.batch:
                            self._residence_until[key] = self.batch.cutoff + residence.get(
                                req.selection.universe, timedelta(0)
                            )
        # Holdings/orders can outlive selection. Retain their bookkeeping without
        # treating those instruments as members of a node's declared selection.
        needed = self._retained_instruments()
        self.retained_resources = {
            key
            for key in set(self.owners) | self.retained_resources
            if key not in owners
            and (
                key[1] in needed
                or self.batch
                and self._residence_until.get(key, self.batch.cutoff) > self.batch.cutoff
            )
        }
        self.owners = owners

    def apply(self, batch: EventBatch) -> list[Event]:
        """Admit a runtime boundary, refresh membership and recompute resource owners.

        Returns:
            Facts this admission itself discovered, currently one ``universe``
            event per named universe whose membership changed. The runtime adds
            them to the batch so the nodes reading that universe activate now.
        """
        # Establish graph/receipt boundaries, then refresh membership and resource
        # ownership before nodes create Contexts for this admission.
        self.batch = batch
        if not self.ticks or batch.cutoff > self.ticks[-1]:
            self.ticks.append(batch.cutoff)
        discovered: list[Event] = []
        for update in self.resources.values():
            for req in update.requirements:
                if not isinstance(req, UniverseRequirement):
                    continue
                definition = req.definition
                # A universe refreshes only when its declared trigger fired in this batch.
                if not any(definition.refresh.matches(event) for event in batch.events):
                    continue
                # A matching trigger still needs selection rows whose own cutoff
                # is available at this graph boundary.
                source = self.selection_inputs.get(definition.name)
                if source is not None and source[1] <= batch.cutoff:
                    old = self.memberships.get(definition.name)
                    try:
                        members = select(definition, list(source[0]), source[1])
                        self.memberships[definition.name] = UniverseMembership(
                            definition.name, members, source[1]
                        )
                    except NotReady:
                        # Missing inputs are explicitly unready, not a valid empty
                        # universe that a strategy can interpret as its selection.
                        self.memberships[definition.name] = UniverseMembership(
                            definition.name, (), source[1], False
                        )
                    # Runtime can react to membership changes discovered during
                    # provider admission through these additional facts.
                    if old != self.memberships[definition.name]:
                        discovered.append(Event("universe", key=definition.name))
        # Update metadata under stable identities. This does not apply simulated
        # cash/quantity corporate-action adjustments to a broker account.
        for event in batch.events:
            life = event.payload
            if isinstance(life, LifecycleEvent) and life.instrument in self.instruments:
                instrument = self.instruments[life.instrument]
                if life.kind == "delisting":
                    self.instruments[life.instrument] = replace(instrument, tradable=False)
                elif life.kind == "symbol_change":
                    self.instruments[life.instrument] = replace(instrument, ticker=life.new_ticker)
        self._rebuild_owners()
        self._changed()
        return discovered

    def _data_reset(self, instrument, keys, effective_at) -> Event | None:
        """Describe rebuilt resources as a DataReset fact for the runtime.

        Args:
            instrument: Instrument whose data was rebuilt.
            keys: ``resource_key`` identities of the rebuilt backing data.
            effective_at: Aware time from which the rebuilt data applies.

        Returns:
            An event naming every node that declared those resources, or None
            when no node did. A subclass appends it to what ``apply`` returns.

        Deciding WHETHER to rebuild is the subclass's job, because it depends on
        how that vendor's data behaves. This helper only answers "which nodes
        were reading what I just rebuilt?" from the shared ownership map.
        """
        nodes = sorted({node for key in keys for node in self.owners.get(key, ())})
        if not nodes:
            return None
        return Event(
            "data_reset",
            instrument,
            payload=DataReset(instrument, tuple(nodes), effective_at),
        )

    def context(self, batch: EventBatch | None = None, node_id: str = "") -> Context:
        """Bind lazy declaration-guarded reads to the current provider version.

        Args:
            batch: Snapshot time boundaries; defaults to the latest admitted batch.
            node_id: Registered reader; empty ID permits execution account access.

        Returns:
            Context sharing compatible cached reads while retaining per-node guards.

        Raises:
            SnapshotError: No batch establishes a snapshot.
        """
        batch = batch or self.batch
        if batch is None:
            raise SnapshotError("No snapshot established")
        # Capture the provider version now; every later lazy read checks that the
        # underlying admitted state still belongs to this Context.
        version = self.version

        def guard():
            if self.version != version:
                raise SnapshotError("Adapter state changed after Context creation")

        # One facade object plays every service role (market, instruments, ...)
        # for this Context. The facade needs the Context for its time boundaries,
        # and the Context needs factories that return the facade. The one-element
        # list below breaks that circle: it is filled in just after the Context is
        # built, and the factories only run later, on first service access.
        facade: list[_ContextServices] = []
        names = (
            ("market", "instruments", "portfolio", "costs", "valuation")
            if self.account_services
            else ("market", "instruments")
        )
        factories = {name: (lambda: facade[0]) for name in names}
        factories["constraints"] = lambda: self.constraints
        # Contexts of one provider version share the provider's cache, so two nodes
        # reading the same bars fetch them once. Each node still passes its own
        # declaration guard before it sees any cached value.
        ctx = Context(batch, factories, guard, node_id, cache=self._snapshot_cache)
        # Creating the facade does not fetch market observations or account state.
        facade.append(_ContextServices(self, ctx, node_id))
        return ctx

    def ready(self, node_id: str, ctx: Context) -> None:
        """Check declared market data and instrument readiness and record the result.

        Raises:
            NotReady: History, freshness, membership, or indicator readiness is incomplete.
            CapabilityError: A declared resource is unsupported.
        """
        # Probe readiness through the same guarded services that node evaluation
        # uses. Missing inputs mean warming; unsupported requests stay visible.
        try:
            self._ready(node_id, ctx)
        except NotReady:
            self.readiness[node_id] = "warming"
            raise
        except CapabilityError:
            self.readiness[node_id] = "unsupported"
            raise
        else:
            self.readiness[node_id] = "ready"

    def _ready(self, node_id, ctx):
        # By default require every selected instrument rather than silently shrinking
        # a declared universe to the subset whose history happens to be ready.
        for req in self.resources[node_id].requirements:
            if (
                isinstance(
                    req,
                    (
                        BarsRequirement,
                        QuotesRequirement,
                        IndicatorRequirement,
                        FundamentalsRequirement,
                        ShortAvailabilityRequirement,
                    ),
                )
                and not req.require_all
            ):
                # The node has taken responsibility for a partial selection: it
                # evaluates and asks ctx.market.ready(req) which members it can use.
                # Resolving still proves that a named universe itself is available,
                # which no node can work without.
                ctx.market.resolve(req.selection)
                continue
            if isinstance(req, (BarsRequirement, QuotesRequirement)):
                for instrument in ctx.market.resolve(req.selection):
                    reader = (
                        ctx.market.bars if isinstance(req, BarsRequirement) else ctx.market.quotes
                    )
                    reader(req, instrument)
            elif isinstance(req, IndicatorRequirement):
                for instrument in ctx.market.resolve(req.selection):
                    if not ctx.market.indicator(req, instrument).ready:
                        raise NotReady("Indicator or requested output window not ready")
            elif isinstance(req, FundamentalsRequirement):
                for instrument in ctx.market.resolve(req.selection):
                    ctx.market.fundamentals(req, instrument)
            elif isinstance(req, ShortAvailabilityRequirement):
                for instrument in ctx.market.resolve(req.selection):
                    ctx.market.short_availability(req, instrument)
            elif isinstance(req, UniverseRequirement):
                ctx.market.membership(req.definition.name)
            elif isinstance(req, InstrumentRequirement):
                for instrument in ctx.market.resolve(req.selection):
                    ctx.instruments.get(instrument)

    def _retained_instruments(self):
        return set()

    # Concrete providers implement these backing hooks. There is no implicit
    # formula fallback or invented data when a provider lacks the capability.
    def _validate_indicator(self, requirement):
        raise CapabilityError("Provider has no native indicator implementation")

    def _indicator(self, requirement, instrument, cutoff):
        raise CapabilityError("Provider has no native indicator implementation")

    def _data(self, requirement, instrument, cutoff):
        raise CapabilityError("Market data source unavailable")

    # Identifies the provider's field catalogue in cached reads; a provider whose
    # catalogue changes between versions sets it.
    fundamental_catalogue_version = None

    def _supports_fundamental_field(self, name):
        """Whether the provider can supply the generic fundamental field ``name``."""
        return False

    def _fundamentals(self, instrument, cutoff, fields=()):
        """The latest FundamentalRecord known at ``cutoff`` with exactly ``fields``, or None."""
        raise CapabilityError("Provider has no fundamental data")

    def _short_availability(self, instrument, cutoff):
        """The latest ShortAvailability known at ``cutoff``, or None."""
        raise CapabilityError("Provider has no borrow data")

    def _session_times(self, instrument, day):
        raise CapabilityError("Provider calendar unavailable")

    def _session_close(self, instrument, after, count):
        raise CapabilityError("Provider calendar unavailable")

    def _session_open(self, instrument, after, count):
        raise CapabilityError("Provider calendar unavailable")

    def _session_bounds(self, instrument, at, extended):
        """Open and close of the session containing ``at``, or the latest one before it."""
        raise CapabilityError("Provider calendar unavailable")

    def _first_tick(self, at):
        """The first admitted clock tick at or after ``at``, or None."""
        index = bisect_left(self.ticks, at)
        return self.ticks[index] if index < len(self.ticks) else None

    def schedule_due(self, schedule: Schedule, after: datetime | None, through: datetime) -> bool:
        """Check a local scheduled occurrence against a data-clock interval.

        Args:
            schedule: Wall time or exchange-session anchor plus optional offset.
            after: Exclusive prior graph time; None checks the startup local day.
            through: Inclusive current graph time.

        Returns:
            Whether at least one occurrence was crossed; missed occurrences coalesce.
        """
        try:
            zone = UTC if schedule.timezone == "UTC" else ZoneInfo(schedule.timezone)
        except ZoneInfoNotFoundError as error:
            raise CapabilityError("Schedule timezone unavailable") from error
        local = through.astimezone(zone)
        after = after or datetime.combine(local.date(), datetime.min.time(), zone)
        # Offsets can cross midnight; include neighboring days when looking for a
        # scheduled occurrence crossed by this observation-clock interval.
        day = (after.astimezone(zone) - abs(schedule.offset)).date()
        last = (local + abs(schedule.offset)).date()
        while day <= last:
            session = self._session_times(schedule.calendar, day) if schedule.calendar else None
            if schedule.calendar and session is None:
                day += timedelta(days=1)
                continue
            if schedule.anchor:
                occurrence = session[0 if schedule.anchor == "open" else 1] + schedule.offset
            else:
                occurrence = datetime.combine(day, schedule.at, zone) + schedule.offset
            # Open-left/closed-right prevents duplicate boundary activation.
            # Several crossed occurrences coalesce into one decision opportunity.
            if after < occurrence.astimezone(UTC) <= through:
                return True
            day += timedelta(days=1)
        return False


class _ContextServices:
    """Declaration-guarded facade shared by the Context's lazy service properties."""

    def __init__(self, owner, ctx, node):
        self.owner, self.ctx, self.node = owner, ctx, node

    def _check(self, requirement=None):
        # Lifetime validation precedes permission/cache checks: authorization does
        # not make a Context valid after a provider mutation.
        self.ctx.check()
        if self.node and requirement is not None:
            if requirement not in self.owner.resources[self.node].requirements:
                raise CapabilityError("Undeclared requirement access")

    def _declares(self, kind, instrument=None):
        self._check()
        # Empty reader identity permits execution account access. Named graph
        # nodes need an explicit matching declaration.
        if not self.node:
            return
        requests = [r for r in self.owner.resources[self.node].requirements if isinstance(r, kind)]
        if instrument is not None:
            requests = [
                r
                for r in requests
                if not r.selection.instruments
                and r.selection.universe is None
                or instrument in self.resolve(r.selection)
            ]
        if not requests:
            raise CapabilityError(f"Undeclared {kind.__name__} access")

    def resolve(self, selection):
        self._check()
        if self.node and not any(
            getattr(r, "selection", None) == selection
            for r in self.owner.resources[self.node].requirements
        ):
            raise CapabilityError("Undeclared selection access")
        # Resolve the admitted dynamic membership, not current/future constituents
        # from an external source or the contents of a previous selection.
        return (
            self.membership(selection.universe).members
            if selection.universe
            else selection.instruments
        )

    def membership(self, name):
        self._check()
        if self.node and not any(
            (isinstance(r, UniverseRequirement) and r.definition.name == name)
            or getattr(getattr(r, "selection", None), "universe", None) == name
            for r in self.owner.resources[self.node].requirements
        ):
            raise CapabilityError("Undeclared universe access")
        # A valid empty membership can be ready; absent, explicitly unready or
        # future-dated membership cannot support this decision boundary.
        value = self.owner.memberships.get(name)
        if value is None or not value.ready or value.cutoff > self.ctx.now:
            raise NotReady(f"Universe unavailable: {name}")
        return value

    def _read(self, req, instrument):
        self._check(req)
        if instrument not in self.resolve(req.selection):
            raise CapabilityError("Instrument outside declared selection")
        # Cache compatible backing values, not the per-reader slice. The same
        # retrieval can serve different declared depths and freshness limits.
        key = resource_key(req, instrument)
        values = self.ctx.compute(
            "market",
            (key,),
            (),
            "1",
            lambda: tuple(self.owner._data(req, instrument, self.ctx.now)),
        )
        # Two clocks gate visibility: observation end <= graph time and content
        # receipt <= available_at. A stored future bar and a late revision fail
        # different halves of this causal admission check.
        values = tuple(
            x
            for x in values
            if x.available_at <= self.ctx.available_at
            and (x.end if isinstance(req, BarsRequirement) else x.time) <= self.ctx.now
            and (req.fill_forward or not getattr(x, "fill_forward", False))
        )
        # Apply lookback after filtering. Missing/stale history raises NotReady;
        # no padding or nearby sample is invented to satisfy the requirement.
        values = values[-req.lookback :]
        if len(values) < req.lookback:
            raise NotReady(f"Insufficient history: {instrument.value}")
        last = values[-1].end if isinstance(req, BarsRequirement) else values[-1].time
        if self.ctx.now - last > req.max_age:
            raise NotReady(f"Stale data: {instrument.value}")
        return values

    def bars(self, requirement, instrument):
        return self._read(requirement, instrument)

    def fundamentals(self, requirement, instrument):
        """The selected security's latest fundamental record known at graph time."""
        self._check(requirement)
        if instrument not in self.resolve(requirement.selection):
            raise CapabilityError("Instrument outside declared selection")
        # Graph and execution Contexts can share a sequence number at different times, so
        # the read time is part of the cached computation's identity.
        # The declared fields and the provider's catalogue version are part of the
        # identity: two readers of one security with different field lists each get theirs.
        record = self.ctx.compute(
            "fundamentals", (instrument,),
            (self.ctx.now, requirement.fields, self.owner.fundamental_catalogue_version), "2",
            lambda: self.owner._fundamentals(instrument, self.ctx.now, requirement.fields))
        # A record dated after graph time is never shown; a missing or old one is not
        # replaced by a guess.
        if record is None or record.time > self.ctx.now:
            raise NotReady(f"No fundamental record: {instrument.value}")
        if self.ctx.now - record.time > requirement.max_age:
            raise NotReady(f"Stale fundamental record: {instrument.value}")
        return record

    def short_availability(self, requirement, instrument):
        """The selected security's latest borrow terms known at graph time."""
        self._check(requirement)
        if instrument not in self.resolve(requirement.selection):
            raise CapabilityError("Instrument outside declared selection")
        record = self.ctx.compute("short_availability", (instrument,), (self.ctx.now,), "1",
                                  lambda: self.owner._short_availability(instrument, self.ctx.now))
        if record is None or record.time > self.ctx.now:
            raise NotReady(f"No borrow record: {instrument.value}")
        return record

    def quotes(self, requirement, instrument):
        return self._read(requirement, instrument)

    def ready(self, requirement):
        """Return the selected instruments whose declared read works right now.

        For example, a momentum node over a 500-name universe declares its bars
        with ``require_all=False``. A stock listed last week lacks the history,
        so it is simply absent from this result and the node ranks the rest.
        """
        usable = []
        for instrument in self.resolve(requirement.selection):
            try:
                if isinstance(requirement, IndicatorRequirement):
                    # An indicator read succeeds while still warming up; it reports
                    # that through its ready flag rather than by raising.
                    if not self.indicator(requirement, instrument).ready:
                        continue
                elif isinstance(requirement, FundamentalsRequirement):
                    self.fundamentals(requirement, instrument)
                elif isinstance(requirement, ShortAvailabilityRequirement):
                    self.short_availability(requirement, instrument)
                else:
                    self._read(requirement, instrument)
            except NotReady:
                # Too little or stale history for this one instrument. Undeclared
                # or unsupported reads raise CapabilityError and are not hidden.
                continue
            usable.append(instrument)
        return tuple(usable)

    def indicator(self, requirement, instrument):
        self._check(requirement)
        if instrument not in self.resolve(requirement.selection):
            raise CapabilityError("Instrument outside declared selection")
        result = self.ctx.compute(
            "indicator",
            (requirement, instrument),
            (),
            "1",
            lambda: self.owner._indicator(requirement, instrument, self.ctx.now),
        )
        # Native state belongs to the provider, but shared reads still reject
        # future indicator outputs and enforce source freshness.
        if any(point.time > self.ctx.now for point in result.points):
            raise SnapshotError("Indicator contains unadmitted future values")
        if result.ready and (
            not result.points or self.ctx.now - result.points[-1].time > requirement.source.max_age
        ):
            raise NotReady("Indicator is stale")
        return result

    def anchor(self, instrument, at, convention):
        self._check()
        # Anchors need an authorized source with the requested return convention.
        # This method neither manufactures adjustments nor shifts target horizons.
        normalization = {
            "price_return": ("raw", "split_adjusted"),
            "total_return": ("total_return", "adjusted"),
        }.get(convention)
        if normalization is None:
            raise CapabilityError("Unsupported anchor convention")
        reqs = [
            r
            for u in (
                [self.owner.resources[self.node]] if self.node else self.owner.resources.values()
            )
            for r in u.requirements
            if isinstance(r, BarsRequirement) and r.normalization in normalization
        ]
        # The price at ``at`` is read at the first clock tick at or after it; see
        # price_at_tick for how a tick without a trade of this instrument is treated.
        tick = self.owner._first_tick(at)
        if tick is None or tick > self.ctx.now:
            raise NotReady("The clock has not reached the anchor time")
        failure = NotReady("No convention-compatible bars for the anchor")
        for req in reqs:
            if instrument not in self.resolve(req.selection):
                continue
            try:
                bars = self._read(req, instrument)
                opened, closes = self.owner._session_bounds(
                    instrument, tick, req.session == "extended"
                )
                # Without history back to the session open, an earlier trade in the
                # session cannot be ruled out, so cases 2 and 3 cannot be told apart.
                if bars[0].end > opened and not any(
                    opened < b.end <= tick and not b.fill_forward for b in bars
                ):
                    raise NotReady("Readable history does not reach the session open")
                return price_at_tick(bars, tick, opened, closes, self.ctx.now)[0]
            except NotReady as error:
                failure = error
        raise failure

    def snapshot(self):
        self._declares(PortfolioRequirement)
        # Cache the authoritative account read once, then restrict what a named
        # node receives. Shared retrieval must not expand node authorization.
        snapshot = self.ctx.compute("account", (), (), "1", self.owner._account)
        if not self.node:
            return snapshot
        reqs = [
            r
            for r in self.owner.resources[self.node].requirements
            if isinstance(r, PortfolioRequirement)
        ]
        if any(not r.selection.instruments and r.selection.universe is None for r in reqs):
            return snapshot
        # Filter instrument-specific holdings/marks/orders by declared selection.
        # NAV/cash keep their account-wide meaning, not a selected-asset subtotal.
        ids = {i for r in reqs for i in self.resolve(r.selection)}
        return replace(
            snapshot,
            holdings={i: q for i, q in snapshot.holdings.items() if i in ids},
            prices={i: p for i, p in snapshot.prices.items() if i in ids},
            open_orders=tuple(o for o in snapshot.open_orders if o.instrument in ids),
        )

    def get(self, instrument):
        self._declares(InstrumentRequirement, instrument)
        return self.owner._instrument(instrument)

    def is_open(self, instrument, at, session):
        self._declares(InstrumentRequirement, instrument)
        return self.owner._open(instrument, at, session)

    def session_close(self, instrument, after, count=1):
        self._declares(InstrumentRequirement, instrument)
        # bool is a subclass of int in Python; reject it so True is not read as 1.
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ContractError("Session count must be a positive integer")
        return self.owner._session_close(instrument, after, count)

    def session_open(self, instrument, after, count=1):
        self._declares(InstrumentRequirement, instrument)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ContractError("Session count must be a positive integer")
        return self.owner._session_open(instrument, after, count)

    def buying_power(self, instrument, quantity):
        self._declares(PortfolioRequirement, instrument)
        return self.owner._buying_power(instrument, quantity)

    def borrow(self, instrument, quantity):
        self._declares(PortfolioRequirement, instrument)
        return self.owner._borrow(instrument, quantity)

    def estimate(self, instrument, quantity, price):
        self._declares(InstrumentRequirement, instrument)
        return self.owner._cost_estimate(instrument, quantity, price)

    def unit_value(self, instrument):
        self._declares(InstrumentRequirement, instrument)
        self._declares(PortfolioRequirement, instrument)
        account = self.ctx.compute("account", (), (), "1", self.owner._account)
        item = self.owner._instrument(instrument)
        # Unit valuation needs account-currency prices here. Cross-currency assets
        # require an explicit FX capability rather than an assumed conversion rate.
        if item.currency != account.currency:
            raise CapabilityError("FX valuation source unavailable")
        price = account.prices.get(instrument)
        if price is None or price <= 0:
            raise NotReady("No valid instrument valuation")
        return price * item.multiplier
