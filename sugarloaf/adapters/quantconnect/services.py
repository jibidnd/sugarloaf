"""Lazy services over LEAN, with bounded shared consolidation windows only."""

from collections import deque
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import isfinite

from sugarloaf.adapters.base import ProviderServices, SelectionRow, resource_key, select
from sugarloaf.core.errors import CapabilityError, NotReady
from sugarloaf.core.events import Event, EventBatch, LifecycleEvent
from sugarloaf.core.primitives import InstrumentId, frozen_map
from sugarloaf.core.services import AccountSnapshot, OpenOrder
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    Capability,
    FundamentalRecord,
    FundamentalUniverse,
    IndicatorPoint,
    IndicatorRequirement,
    IndicatorSnapshot,
    Instrument,
    LiquidityUniverse,
    Quote,
    QuotesRequirement,
    ResourceUpdate,
    Selection,
    ShortAvailability,
    Trigger,
    UniverseMembership,
    UniverseRequirement,
)

from . import fundamental_catalogue as catalogue
from .lean import DAY, MINUTE, LeanAPI, native_period


def utc(value: datetime, zone: str = "UTC") -> datetime:
    """Normalize LEAN utc_time to an aware UTC datetime.

    Naive input is interpreted as UTC. Exchange/bar timestamps require LEAN's
    timezone conversion rather than this helper.

    Raises:
        CapabilityError: A zone other than UTC is requested.
    """
    if zone != "UTC":
        raise CapabilityError("Use platform exchange timezone conversion for local timestamps")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class QuantConnectDataServices(ProviderServices):
    """Own bounded native resources; expose neutral values only after admission.

    Platform callbacks queue completed observations. ``apply`` reconciles resource
    ownership before admitting that queue; Context construction stays lazy.
    """

    def __init__(
        self,
        algorithm,
        symbols=None,
        constraints=None,
        api=None,
        cost_rate=0.0005,
        leverage=1.0,
        account_services=False,
        borrow_data=None,
    ):
        # ProviderServices owns declaration checks, Context versions and readiness;
        # this subclass supplies the native resources behind its lazy read hooks.
        super().__init__(constraints=constraints, account_services=account_services)
        self.cost_rate = cost_rate
        self.algorithm = algorithm
        self.api = api or LeanAPI()
        self.symbols = dict(symbols or {})
        # Caller-owned symbol bindings must not be released as adapter-owned
        # manual subscriptions when a graph stops requesting an instrument.
        self._external_symbols = set(self.symbols)
        self.reverse = {str(symbol.id): instrument for instrument, symbol in self.symbols.items()}
        self.tickers = {}
        # Keys identify compatible resource semantics, so several nodes can share
        # one bounded window/consolidator while retaining separate read permissions.
        # Each window is a deque of neutral Bar/Quote values, oldest first, capped
        # at the largest lookback any reader declared. A native bar is converted
        # once, when it is admitted, and never again on reads.
        self.windows = {}
        self.consolidators = {}
        self.universes = {}
        self.universe_definitions = {}
        # Feed settings each native universe source was created with: (period,
        # session, normalization, fill-forward). A change recreates the source.
        self._universe_feeds = {}
        # Feed period planned for each security at the last resource sync.
        self._feed_periods = {}
        self._selected_at = {}
        self.pending_events = []
        self.delisted = set()
        self._subscriptions = {}
        self._resource_epoch = 0
        self._retired_universes = []
        self._universe_generations = {}
        self.native_indicators = {}
        self.indicator_diagnostics = deque(maxlen=200)
        # Native callbacks stage data here. Admission occurs at apply(), after
        # lifecycle resets; receipt timestamps remain separate from observation ends.
        self._pending_data = []
        self._prefilling = False
        if account_services:
            self.api.setup(algorithm, cost_rate, leverage, borrow_data)

    def bind(self, symbol) -> InstrumentId:
        """Preserve identity across ticker changes using the platform security ID."""
        # The platform SID survives display-ticker changes. Preserve an explicit
        # strategy InstrumentId if that SID was already bound by subscription setup.
        key = str(symbol.id)
        instrument = self.reverse.get(key, InstrumentId(key))
        self.reverse[key] = instrument
        self.symbols[instrument] = symbol
        return instrument

    def register(self, instrument: InstrumentId, ticker: str) -> None:
        """Supply a ticker for initial subscription; existing native bindings follow SID."""
        self.tickers[instrument] = ticker

    def install(self, node_id: str, update: ResourceUpdate) -> None:
        """Validate declarations and replace universe sources at the runtime boundary."""
        for requested in update.requirements:
            req = requested.source if isinstance(requested, IndicatorRequirement) else requested
            if isinstance(req, (BarsRequirement, QuotesRequirement)):
                if req.interval < timedelta(minutes=1) or req.interval % timedelta(minutes=1):
                    raise CapabilityError("US equity adapter requires whole-minute intervals")
                if req.interval > timedelta(days=1):
                    raise CapabilityError("QC consolidations are at most one day")
            if isinstance(req, UniverseRequirement):
                self._validate_universe(req.definition)
        # Install a complete declaration replacement through shared validation.
        # This rebuilds ownership and invalidates Contexts from older declarations.
        super().install(node_id, update)
        # Sources are shared by neutral universe name. Keep a source while any
        # remaining declaration defines or references that universe.
        definitions = {
            r.definition.name: r.definition
            for update in self.resources.values()
            for r in update.requirements
            if isinstance(r, UniverseRequirement)
        }
        referenced = set(definitions)
        referenced.update(
            getattr(getattr(req, "selection", None), "universe", None)
            for update in self.resources.values()
            for req in update.requirements
        )
        for definition in definitions.values():
            self._install_universe(definition)
        for name in set(self.universes) - referenced:
            self._retire_universe(name)

    def _validate_universe(self, definition):
        if definition.refresh != Trigger("universe"):
            raise CapabilityError(
                "QC selection sources support only the default daily universe "
                "refresh; keyed, interval and scheduled refreshes are unsupported"
            )
        expected = (
            "us_equity_liquidity"
            if isinstance(definition, LiquidityUniverse)
            else "us_equity_fundamental"
            if isinstance(definition, FundamentalUniverse)
            else "historical_etf_constituents"
        )
        if definition.source != expected:
            raise CapabilityError("Unsupported QC universe source")

    def _retire_universe(self, name):
        # Retiring selection ownership does not liquidate its members. LEAN may
        # retain held/open-order securities until its disposed universe can drain.
        universe = self.universes.pop(name)
        self.api.dispose_universe(universe)
        self._retired_universes.append((self._resource_epoch, universe))
        self.universe_definitions.pop(name, None)
        self._universe_feeds.pop(name, None)
        self.memberships.pop(name, None)
        self._selected_at.pop(name, None)
        # Reject callbacks captured by the old source after its logical retirement.
        self._universe_generations[name] = self._universe_generations.get(name, 0) + 1
        self.pending_events.append(Event("universe", key=name))

    def _universe_feed(self, name):
        """The requirement that reads a universe's members, and the feed period they need.

        Returns:
            (feed, period): ``feed`` is a bars requirement selecting the universe
            (a quotes requirement if there are no bars, None if no node reads the
            members' prices); ``period`` is the finest ``native_period`` among them,
            or one day when nothing reads prices.
        """
        feed, period = None, DAY
        for update in self.resources.values():
            for requested in update.requirements:
                req = requested.source if isinstance(requested, IndicatorRequirement) else requested
                if (
                    not isinstance(req, (BarsRequirement, QuotesRequirement))
                    or req.selection.universe != name
                ):
                    continue
                period = min(period, native_period(req))
                if feed is None or (
                    isinstance(req, BarsRequirement) and not isinstance(feed, BarsRequirement)
                ):
                    feed = req
        return feed, period

    def _install_universe(self, definition):
        # LEAN subscribes each member it selects. Give the source the same feed
        # settings the members' readers ask for, so a daily strategy gets daily
        # members and each member carries a single subscription.
        feed, period = self._universe_feed(definition.name)
        settings = (
            period,
            getattr(feed, "session", "regular"),
            getattr(feed, "normalization", "adjusted"),
            bool(getattr(feed, "fill_forward", False)),
        )
        if (
            self.universe_definitions.get(definition.name) == definition
            and self._universe_feeds.get(definition.name) == settings
        ):
            return
        self._validate_universe(definition)
        # Each LEAN source factory generates a unique platform universe symbol.
        # A neutral name can therefore replace its source while old held members drain.
        generation = self._universe_generations.get(definition.name, 0) + 1

        def selector(data):
            # The same neutral name can outlive several native sources. Only the
            # current generation is allowed to publish membership for that name.
            if self._universe_generations.get(definition.name) != generation:
                return []
            now = utc(self.algorithm.utc_time, "UTC")
            # LEAN may call twice at one selection time. Keep no-update distinct from empty.
            if self._selected_at.get(definition.name) == now:
                return self.api.unchanged()
            rows = []
            for item in data:
                instrument = self.bind(item.symbol)
                if isinstance(definition, FundamentalUniverse):
                    # Only the definition's declared fields are read, so a selection over
                    # thousands of securities stays cheap.
                    rows.append(_fundamental_record(instrument, item, now, definition.fields))
                elif isinstance(definition, LiquidityUniverse):
                    rows.append(
                        SelectionRow(
                            instrument,
                            float(item.price),
                            float(item.volume),
                            float(item.dollar_volume),
                            # None when the row does not say, which a universe
                            # that requires fundamentals reports as not ready.
                            has_fundamentals=getattr(item, "has_fundamental_data", None),
                        )
                    )
                else:
                    rows.append(
                        SelectionRow(
                            instrument,
                            weight=(float(item.weight) if item.weight is not None else None),
                        )
                    )
            # Native data supplies available fields; shared selection applies the
            # declared ranking/filter rules with stable identity tie breaking.
            try:
                members = select(definition, rows, now)
            except NotReady:
                # As in the shared path: the universe is unready, not empty. Members keep
                # their subscriptions until a selection can be made.
                self.memberships[definition.name] = UniverseMembership(
                    definition.name, (), now, False
                )
                self._selected_at[definition.name] = now
                self.pending_events.append(Event("universe", key=definition.name))
                self._changed()
                return self.api.unchanged()
            self.memberships[definition.name] = UniverseMembership(definition.name, members, now)
            self._selected_at[definition.name] = now
            # Publish membership now and queue a trigger for the next data boundary;
            # a selection callback does not evaluate the graph on its own.
            self.pending_events.append(Event("universe", key=definition.name))
            self._changed()
            return [self.symbols[i] for i in members]

        ticker = None
        if not isinstance(definition, (LiquidityUniverse, FundamentalUniverse)):
            symbol = self.symbols.get(definition.etf)
            ticker = symbol.value if symbol is not None else self.tickers.get(definition.etf)
            if ticker is None:
                raise CapabilityError("ETF universe requires a stable-identity ticker binding")
        # Construct the replacement before retiring the prior source. Its closure
        # will become authoritative when the generation is installed below.
        universe = self.api.universe(self.algorithm, definition, selector, ticker, feed, period)
        if definition.name in self.universes:
            self._retire_universe(definition.name)
        self._universe_generations[definition.name] = generation
        self.universes[definition.name] = universe
        self.universe_definitions[definition.name] = definition
        self._universe_feeds[definition.name] = settings

    def apply(self, batch: EventBatch) -> list[Event]:
        """Admit queued inputs after lifecycle resets, ownership changes, and native warmup.

        Returns:
            Shared admission facts plus every fact native callbacks queued since
            the last admission (completed bars, universe selections, schedules).
        """
        # Give retired native universes a later resource boundary to drain; never
        # remove their manager entries merely because graph selection changed.
        self._resource_epoch += 1
        retained = []
        for epoch, universe in self._retired_universes:
            if self._resource_epoch <= epoch + 1 or not self.api.collect_universe(
                self.algorithm, universe
            ):
                retained.append((epoch, universe))
        self._retired_universes = retained
        # Shared admission establishes the batch, refreshes owners and invalidates
        # stale Contexts before any graph reads can observe the new boundary.
        events = super().apply(batch)
        events.extend(self.pending_events)
        self.pending_events.clear()
        # Decide, for this vendor, which retained data a corporate action made
        # unusable. Drop it here; _sync_resources below rebuilds it from history
        # before queued new samples are admitted. Then tell the runtime which
        # nodes were reading it, so their outputs are withdrawn and recomputed.
        for event in batch.events:
            life = event.payload
            if not isinstance(life, LifecycleEvent):
                continue
            rebuilt = set()
            for key in tuple(self.windows):
                if self._rebuilds(key, life):
                    self.algorithm.subscription_manager.remove_consolidator(
                        self.symbols[key[1]], self.consolidators.pop(key)
                    )
                    del self.windows[key]
                    rebuilt.add(key)
            for key in tuple(self.native_indicators):
                # An indicator key ends with the resource key of its source bars.
                if self._rebuilds(key[3], life):
                    del self.native_indicators[key]
                    rebuilt.add(key[3])
            reset = self._data_reset(life.instrument, rebuilt, life.effective_at)
            if reset is not None:
                events.append(reset)
        # Resource changes and silent prefill happen at this cutoff. Only after
        # that setup can pending completed observations feed the surviving state.
        self._sync_resources(batch.cutoff)
        self._admit_pending_data()
        self._changed()
        return events

    @staticmethod
    def _rebuilds(key, life) -> bool:
        """Whether a lifecycle fact makes LEAN data held under a resource key unusable.

        ``key`` is a ``resource_key`` tuple: position 1 is the instrument and
        position 4 the price normalization (None for a requirement that has none).

        This rule describes QuantConnect backtests and research, the only modes
        this adapter supports. It is deliberately here and not in the core:
        another vendor's data may behave differently.

        * Dividend: nothing is rebuilt, under any normalization. LEAN's adjusted
          and total-return prices are adjusted with the whole factor file from
          the first bar, so they are already continuous through the ex-date and
          do not change on it. Raw and split-adjusted prices simply trade lower
          by the dividend, which is a real price move, and the cash is credited
          by LEAN. (Observed natively: an adjusted QQQ window was identical
          before and after its ex-dividend date.)
        * Split, adjusted normalizations: nothing is rebuilt, for the same
          reason. The series is continuous through the split.
        * Split, raw prices or raw quotes: rebuilt. Prices before and after the
          split are on different per-share scales. Note the rebuilt history is
          still as traded, so a window spanning the split mixes both scales;
          the rebuild restarts indicators, it does not rescale old prices.
          Quotes declare their normalization like bars (raw by default); adjusted
          quotes are continuous through a split and are not rebuilt.
        """
        return key[1] == life.instrument and life.kind == "split" and key[4] in (None, "raw")

    def _admit_pending_data(self):
        """Admit completed native inputs after the batch's lifecycle reset."""
        # Detach the queue so any later callback belongs to a subsequent admission.
        pending, self._pending_data = self._pending_data, []
        for key, bar, received in pending:
            self._admit_bar(key, bar, received)

    def _sync_resources(self, cutoff):
        """Share compatible windows, warm new resources, then release unused owners."""
        desired = {}
        # Securities a node names itself, as opposed to members a universe supplies.
        explicit = set()
        for update in self.resources.values():
            for requested in update.requirements:
                req = requested.source if isinstance(requested, IndicatorRequirement) else requested
                if not isinstance(req, (BarsRequirement, QuotesRequirement)):
                    continue
                membership = self.memberships.get(req.selection.universe)
                ids = membership.members if membership else req.selection.instruments
                if not membership:
                    explicit.update(ids)
                for instrument in ids:
                    key = resource_key(req, instrument)
                    # Backing storage serves the largest compatible requested
                    # lookback; shared Context checks still bound each node's read.
                    if key not in desired or desired[key].lookback < req.lookback:
                        desired[key] = req
        # A schedule is checked whenever data arrives, so the security whose calendar
        # it uses must tick at least once a minute. It owns a minute feed even when
        # no node reads that security's prices; declaration checks still prohibit
        # such reads.
        calendars = set()
        for update in self.resources.values():
            for trigger in update.triggers:
                calendar = trigger.schedule.calendar if trigger.schedule else None
                if calendar is None:
                    continue
                calendars.add(calendar)
                if not any(key[1] == calendar for key in desired):
                    request = BarsRequirement(Selection((calendar,)), MINUTE, normalization="raw")
                    desired[resource_key(request, calendar)] = request
        # Each security is fed at the finest period any of its readers needs, and no
        # finer: a node that reads daily bars does not cause a minute feed.
        periods = {}
        for key, req in desired.items():
            periods[key[1]] = min(periods.get(key[1], DAY), native_period(req))
        # LEAN reads a consolidator from the first subscription a security was given
        # and ignores resolution. A security named explicitly is subscribed before
        # any universe selects it, so give it at once the finest feed it could need
        # as a member; a later, coarser universe subscription then changes nothing.
        finest_universe = min((feed[0] for feed in self._universe_feeds.values()), default=DAY)
        for instrument in explicit | calendars:
            periods[instrument] = min(periods[instrument], finest_universe)
        for calendar in calendars:
            periods[calendar] = MINUTE
        for instrument, period in periods.items():
            # A feed is never made coarser while something still reads the security.
            current = self._subscriptions.get(instrument)
            if current is not None:
                periods[instrument] = min(period, current[3])
        self._feed_periods = periods
        # Indicator prefill has its own native warmup length. Window prefill below
        # suppresses indicator updates to avoid feeding the same history twice.
        self._sync_indicators(cutoff, periods)
        # LEAN equity securities cannot carry incompatible adjustment/session subscriptions here.
        by_instrument = {}
        primary = {}
        for key, req in desired.items():
            if key[1] not in primary or isinstance(req, BarsRequirement):
                primary[key[1]] = req
        for key, req in desired.items():
            normalization = getattr(
                req, "normalization", getattr(primary[key[1]], "normalization", "raw")
            )
            # A single native equity subscription must have one session, price
            # adjustment and fill-forward policy, even across different intervals.
            semantics = (req.session, normalization, req.fill_forward)
            prior = by_instrument.setdefault(key[1], semantics)
            if prior != semantics:
                raise CapabilityError("Incompatible subscriptions for one LEAN equity security")
        for instrument, semantics in by_instrument.items():
            semantics = (*semantics, periods[instrument])
            if self._subscriptions.get(instrument) == semantics:
                continue
            # The native subscription itself is changing (session, adjustment,
            # fill-forward or feed period), so every window built on the old one is
            # discarded.
            for key in tuple(self.windows):
                if key[1] == instrument:
                    self.algorithm.subscription_manager.remove_consolidator(
                        self.symbols[instrument], self.consolidators.pop(key)
                    )
                    del self.windows[key]
            # Reuse the current mapped ticker when rebinding an existing SID.
            # The explicit registration is only needed before the first binding.
            existing = self.symbols.get(instrument)
            ticker = existing.value if existing is not None else self.tickers.get(instrument)
            if ticker is None:
                raise CapabilityError(f"No ticker binding for {instrument.value}")
            period = periods[instrument]
            symbol = self.api.subscribe(self.algorithm, ticker, primary[instrument], period)
            self.symbols[instrument] = symbol
            self.reverse[str(symbol.id)] = instrument
            # The security may already carry a coarser subscription, added by a
            # universe or an earlier declaration, and LEAN would feed consolidators
            # from that one. Fail here instead of handing nodes bars of the wrong
            # interval.
            feeding = self.api.feed_period(
                self.algorithm, symbol, not isinstance(primary[instrument], BarsRequirement)
            )
            if feeding is not None and feeding != period:
                raise CapabilityError(
                    f"{instrument.value} is already subscribed with a {feeding} feed and now "
                    f"needs {period}; request the finer interval from the start"
                )
            self._subscriptions[instrument] = semantics
        # Create or enlarge only the required windows. Replacing a consolidator
        # prevents the old handler from continuing to own the same resource key.
        for key, req in desired.items():
            if key in self.windows and self.windows[key].maxlen >= req.lookback:
                continue
            if key in self.consolidators:
                self.algorithm.subscription_manager.remove_consolidator(
                    self.symbols[key[1]], self.consolidators.pop(key)
                )
            instrument = key[1]
            symbol = self.symbols[instrument]
            period = periods[instrument]
            consolidator = self.api.consolidator(req, self.algorithm.securities[symbol], period)
            # maxlen makes the deque drop its oldest value by itself once the
            # largest declared lookback is reached, so memory stays bounded.
            self.windows[key] = deque(maxlen=req.lookback)
            self.consolidators[key] = consolidator

            # Capture this resource's identifiers now, not the loop's later values.
            # Completion reports a bar end; receipt comes from the provider clock.
            def completed(sender, bar, key=key, req=req, instrument=instrument):
                received = self._receipt_time()
                # Silent prefill populates bounded history immediately. Streaming
                # callbacks only queue facts until the next controlled admission.
                if self._prefilling:
                    self._admit_bar(key, bar, received, indicators=False)
                else:
                    self._pending_data.append((key, bar, received))
                    end = self.api.to_utc(
                        self.algorithm.securities[self.symbols[instrument]], bar.end_time
                    )
                    self.pending_events.append(
                        Event("bar_close", instrument, req.interval, end=end)
                    )
                    self._changed()

            consolidator.data_consolidated += completed
            # Historical retrieval is capped at the explicit observation cutoff.
            # The native history span may still be too short to satisfy readiness.
            self._prefilling = True
            try:
                history = self.api.history(
                    self.algorithm, symbol, req, cutoff, req.lookback, period
                )
                for bar in history:
                    if self.api.to_utc(self.algorithm.securities[symbol], bar.end_time) <= cutoff:
                        # Native regular-session daily history is already
                        # consolidated; other histories feed the interval builder.
                        if req.interval == DAY and req.session == "regular":
                            completed(consolidator, bar)
                        else:
                            consolidator.update(bar)
                self.api.scan(consolidator, self.algorithm.securities[symbol], cutoff)
            finally:
                self._prefilling = False
            # Attach streaming ownership only after silent initialization finishes.
            self.algorithm.subscription_manager.add_consolidator(symbol, consolidator)
        for key in set(self.windows) - set(desired):
            symbol = self.symbols[key[1]]
            self.algorithm.subscription_manager.remove_consolidator(
                symbol, self.consolidators.pop(key)
            )
            del self.windows[key]
        # Dropping graph data ownership does not imply selling a holding. Native
        # release can defer while holdings/open orders still require the security.
        needed_instruments = {key[1] for key in desired}
        for instrument in set(self._subscriptions) - needed_instruments:
            if instrument not in self._external_symbols:
                if not self.api.release_subscription(self.algorithm, self.symbols[instrument]):
                    continue
            del self._subscriptions[instrument]

    def _data(self, req, instrument, cutoff):
        key = resource_key(req, instrument)
        window = self.windows.get(key)
        if window is None:
            raise NotReady("Subscription requested but not warmed")
        self.read_counts[key] = self.read_counts.get(key, 0) + 1
        # The window already holds neutral values in chronological order. Shared
        # services enforce cutoff, receipt, lookback and freshness on every read.
        return tuple(window)

    def _neutral(self, key, bar, received):
        """Convert one completed native bar to a neutral Bar or Quote.

        Args:
            key: Resource key; its first element names the requirement type.
            bar: Native TradeBar or QuoteBar.
            received: When this content became known, used as ``available_at``.

        Returns:
            The neutral value, or None for a quote bar missing its bid or ask.
        """
        instrument = key[1]
        security = self.algorithm.securities[self.symbols[instrument]]
        # Native bar timestamps use the exchange zone, not the algorithm UTC helper.
        end = self.api.to_utc(security, bar.end_time)
        if key[0] == BarsRequirement.__name__:
            return Bar(
                instrument,
                self.api.to_utc(security, bar.time),
                end,
                received,
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
                bool(bar.is_fill_forward),
            )
        if bar.bid is None or bar.ask is None:
            # A one-sided quote bar is not a usable bid/ask observation.
            return None
        return Quote(
            instrument,
            end,
            received,
            float(bar.bid.close),
            float(bar.ask.close),
            float(bar.last_bid_size),
            float(bar.last_ask_size),
        )

    @property
    def provenance(self):
        """Report native indicator settings, initialization, versions, and admission state."""
        # Provenance describes actual native resources and initialization rather
        # than claiming parity with a memory formula or historical data vintage.
        indicators = []
        for key, entry in self.native_indicators.items():
            request = entry["requirement"]
            indicators.append(
                frozen_map(
                    {
                        "kind": key[0],
                        "instrument": key[1].value,
                        "parameters": request.parameters,
                        "source": repr(request.source),
                        "window": entry["size"],
                        "outputs": tuple(entry["outputs"]),
                        "initialization_start": (
                            entry["prefill_start"].isoformat() if entry["prefill_start"] else None
                        ),
                        "last_input_end": entry["last"].isoformat() if entry["last"] else None,
                        "late_input": "native_forward_only",
                    }
                )
            )
        return frozen_map(
            {
                "implementation": "QuantConnect/LEAN",
                "version": self.api.indicator_version(),
                "arrival_clock": "simulated" if self.account_services else "assumed_history",
                "indicators": tuple(indicators),
            }
        )

    def _receipt_time(self):
        return utc(self.algorithm.utc_time, "UTC")

    def _retained_instruments(self):
        if not self.account_services:
            return set()
        # Shared ownership bookkeeping can retain identities needed by account
        # exposure even after they leave a requested universe.
        result = {
            self.bind(symbol)
            for symbol, security in self.algorithm.securities.items()
            if security.holdings.quantity
        }
        result.update(self.bind(o.symbol) for o in self.algorithm.transactions.get_open_orders())
        return result

    def schedule_due(self, schedule, after, through):
        """Delegate crossed-occurrence checks to LEAN exchange calendars and timezone data."""
        return self.api.schedule_due(self.algorithm, self.symbols, schedule, after, through)

    def _validate_indicator(self, requirement):
        # Native creation also validates parameters; do it before resource installation.
        self.api.indicator(requirement)

    @staticmethod
    def _indicator_key(requirement, instrument):
        return (
            requirement.kind,
            instrument,
            requirement.parameters,
            resource_key(requirement.source, instrument),
        )

    def _sync_indicators(self, cutoff, periods):
        # Share equal kind/parameters/source semantics and retain the largest
        # output window requested by any owner. Window size is not indicator identity.
        desired = {}
        for update in self.resources.values():
            for req in update.requirements:
                if not isinstance(req, IndicatorRequirement):
                    continue
                membership = self.memberships.get(req.selection.universe)
                for instrument in membership.members if membership else req.selection.instruments:
                    key = self._indicator_key(req, instrument)
                    if key not in desired or desired[key].window < req.window:
                        desired[key] = req
        for key in set(self.native_indicators) - set(desired):
            del self.native_indicators[key]
        for key, req in desired.items():
            if key in self.native_indicators and self.native_indicators[key]["size"] >= req.window:
                continue
            instrument = key[1]
            symbol = self.symbols.get(instrument)
            if symbol is None:
                ticker = self.tickers.get(instrument)
                if ticker is None:
                    raise CapabilityError("Indicator instrument has no ticker binding")
                symbol = self.api.subscribe(
                    self.algorithm, ticker, req.source, periods[instrument]
                )
                self.symbols[instrument] = symbol
                self.reverse[str(symbol.id)] = instrument
            native, outputs, field = self.api.indicator(req)
            entry = {
                "native": native,
                "outputs": outputs,
                "field": field,
                "requirement": req,
                "size": req.window,
                "last": None,
                "last_identity": None,
                "ready_since": None,
                "prefill_start": None,
            }
            self.native_indicators[key] = entry
            security = self.algorithm.securities[symbol]
            # Becoming ready gives one valid point; a window of N ready points
            # needs N-1 more inputs beyond the native indicator warmup period.
            warmup = int(native.warm_up_period) + req.window - 1
            consolidator = self.api.consolidator(req.source, security, periods[instrument])

            # This temporary consolidator initializes only this native indicator.
            # It is not registered as another streaming update path.
            def warm(sender, bar, key=key):
                if self.api.to_utc(security, bar.end_time) <= cutoff:
                    self._update_indicator(key, bar)

            consolidator.data_consolidated += warm
            for bar in self.api.history(
                self.algorithm, symbol, req.source, cutoff, warmup, periods[instrument]
            ):
                if req.source.interval == DAY and req.source.session == "regular":
                    warm(consolidator, bar)
                else:
                    consolidator.update(bar)
            self.api.scan(consolidator, security, cutoff)

    def _update_indicator(self, key, bar):
        entry = self.native_indicators[key]
        security = self.algorithm.securities[self.symbols[key[1]]]
        end = self.api.to_utc(security, bar.end_time)
        identity = self._observation_identity(bar)
        # Native indicators advance forward. An older admitted bar may revise a
        # data window, but cannot rewind this stateful native calculation. Record
        # that limitation rather than pretending a historical recomputation occurred.
        if entry["last"] is not None and end < entry["last"]:
            self.indicator_diagnostics.append(
                {
                    "code": "NATIVE_INDICATOR_LATE_INPUT",
                    "instrument": key[1].value,
                    "end": end.isoformat(),
                    "indicator_end": entry["last"].isoformat(),
                }
            )
            return
        # Suppress identical repeated inputs, including the warmup/stream overlap.
        # A changed same-end input is not treated as an identical observation.
        if entry["last_identity"] == identity:
            return
        entry["last_identity"] = identity
        if entry["prefill_start"] is None:
            entry["prefill_start"] = end
        self.api.update_indicator(entry["native"], key[0], entry["field"], bar)
        entry["last"] = end
        # Track the first valid output boundary so warmup values cannot later be
        # exposed as if the indicator had already been ready when they were created.
        if entry["native"].is_ready and entry["ready_since"] is None:
            entry["ready_since"] = end

    @staticmethod
    def _observation_identity(bar):
        values = tuple(
            getattr(bar, field, None)
            for field in (
                "time",
                "end_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "last_bid_size",
                "last_ask_size",
                "is_fill_forward",
            )
        )
        return values + tuple(
            getattr(getattr(bar, side, None), "close", None) for side in ("bid", "ask")
        )

    @staticmethod
    def _stamp(value):
        """Return a neutral observation's own timestamp: a bar's end or a quote's time."""
        return value.end if isinstance(value, Bar) else value.time

    def _admit_bar(self, key, bar, received, indicators=True):
        window = self.windows.get(key)
        # A queued callback can belong to a resource whose owner was removed.
        # Do not resurrect that resource merely because its last bar arrived.
        if window is None:
            return
        value = self._neutral(key, bar, received)
        if value is not None:
            stamp = self._stamp(value)
            position = next(
                (i for i, item in enumerate(window) if self._stamp(item) == stamp), None
            )
            if position is not None:
                # Same timestamp as a retained value. A repeat of identical content
                # (for example the overlap between warmup history and the live
                # stream) keeps its original receipt time. Changed content is a
                # revision and takes the receipt time of this new arrival, because
                # observation time alone cannot say when that content was known.
                if replace(window[position], available_at=value.available_at) != value:
                    window[position] = value
            elif window and stamp < self._stamp(window[-1]):
                # A late arrival older than the newest retained value. Insert it in
                # chronological order, then keep the newest maxlen values so the
                # late one cannot push out anything more recent.
                merged = sorted([*window, value], key=self._stamp)[-window.maxlen :]
                window.clear()
                window.extend(merged)
            else:
                # The normal case: a newer observation goes on the end.
                window.append(value)
        # Only the admitted source path updates streaming indicators; prefill
        # can explicitly disable this after their independent initialization.
        if indicators:
            for indicator_key in tuple(self.native_indicators):
                if indicator_key[3] == key:
                    self._update_indicator(indicator_key, bar)

    def _indicator(self, requirement, instrument, cutoff):
        entry = self.native_indicators.get(self._indicator_key(requirement, instrument))
        if entry is None:
            raise NotReady("Indicator resource unavailable")
        security = self.algorithm.securities[self.symbols[instrument]]
        # Assemble named outputs only when their native sample timestamps agree.
        # Exclude pre-readiness and post-cutoff values from the neutral snapshot.
        windows = {name: entry["outputs"][name].window for name in requirement.outputs}
        points = []
        for index in range(min(window.count for window in windows.values())):
            samples = {name: window[index] for name, window in windows.items()}
            times = {self.api.to_utc(security, sample.end_time) for sample in samples.values()}
            if len(times) != 1:
                continue
            at = next(iter(times))
            if entry["ready_since"] is not None and entry["ready_since"] <= at <= cutoff:
                points.append(
                    IndicatorPoint(
                        at, {name: float(sample.value) for name, sample in samples.items()}
                    )
                )
            if len(points) == requirement.window:
                break
        # Native output windows are newest-first; the public result is chronological.
        points = tuple(reversed(points))
        provenance = {
            "provider": "QuantConnect/LEAN",
            "version": self.api.indicator_version(),
            "kind": requirement.kind,
            "parameters": requirement.parameters,
            "source": repr(requirement.source),
            "late_input": "native_forward_only",
            "initialization_start": entry["prefill_start"].isoformat()
            if entry["prefill_start"]
            else None,
            "last_input_end": entry["last"].isoformat() if entry["last"] else None,
            "arrival_clock": "simulated" if self.account_services else "assumed_history",
            "admitted_sequence": self.batch.sequence if self.batch else None,
        }
        # Native readiness and a complete requested output window are separate
        # conditions: both must hold before a node receives a ready snapshot.
        return IndicatorSnapshot(
            points, bool(entry["native"].is_ready and len(points) == requirement.window), provenance
        )

    def _instrument(self, instrument):
        symbol = self.symbols.get(instrument)
        if symbol is None:
            raise CapabilityError("Instrument has no platform Symbol")
        security = self.algorithm.securities[symbol]
        if str(getattr(symbol, "security_type", "Equity")).split(".")[-1].lower() != "equity":
            raise CapabilityError("QC adapter supports US equities only")
        if str(getattr(symbol.id, "market", "usa")).lower() != "usa":
            raise CapabilityError("QC adapter supports US markets only")
        # Expose neutral trade units/metadata while keeping Symbol and native
        # model objects private to this adapter. Delisting overrides tradability.
        props = security.symbol_properties
        return Instrument(
            instrument,
            symbol.value,
            currency=security.quote_currency.symbol,
            lot_size=float(props.lot_size),
            multiplier=float(props.contract_multiplier),
            tradable=security.is_tradable and instrument not in self.delisted,
        )

    def _account(self):
        self.read_counts["account"] = self.read_counts.get("account", 0) + 1
        # Read authoritative LEAN account state lazily for the current Context.
        # Never infer holdings from targets or reconstruct them from event deltas.
        portfolio = self.algorithm.portfolio
        holdings, prices = {}, {}
        for symbol, security in self.algorithm.securities.items():
            instrument = self.bind(symbol)
            if security.holdings.quantity:
                holdings[instrument] = float(security.holdings.quantity)
            if security.price > 0:
                prices[instrument] = float(security.price)
        orders = []
        for order in self.algorithm.transactions.get_open_orders():
            # Pair requested quantity with the native cumulative filled quantity;
            # execution derives remaining exposure without double-counting fills.
            ticket = self.algorithm.transactions.get_order_ticket(order.id)
            orders.append(
                OpenOrder(
                    str(order.id),
                    self.bind(order.symbol),
                    float(order.quantity),
                    float(ticket.quantity_filled),
                    "open",
                    order.tag,
                )
            )
        # Holdings are units, prices are native marks, and NAV/cash remain in
        # LEAN's account conventions. Context validation owns permitted access.
        return AccountSnapshot(
            holdings,
            float(portfolio.total_portfolio_value),
            {k: float(v.amount) for k, v in portfolio.cash_book.items()},
            prices,
            tuple(orders),
            self.algorithm.account_currency,
        )

    def _open(self, instrument, at, session):
        security = self.algorithm.securities[self.symbols[instrument]]
        return self.api.is_open(security, at, session == "extended")

    def _session_close(self, instrument, after, count):
        symbol = self.symbols.get(instrument)
        if symbol is None:
            raise CapabilityError("Instrument has no platform Symbol")
        # LEAN's exchange-hours database owns holidays, early closes and DST.
        return self.api.session_close(self.algorithm.securities[symbol], after, count)

    def _session_bounds(self, instrument, at, extended):
        symbol = self.symbols.get(instrument)
        if symbol is None:
            raise CapabilityError("Instrument has no platform Symbol")
        return self.api.session_bounds(self.algorithm.securities[symbol], at, extended)

    def _session_open(self, instrument, after, count):
        symbol = self.symbols.get(instrument)
        if symbol is None:
            raise CapabilityError("Instrument has no platform Symbol")
        return self.api.session_open(self.algorithm.securities[symbol], after, count)

    def _subscribed(self, instrument):
        symbol = self.symbols.get(instrument)
        if symbol is None or symbol not in self.algorithm.securities:
            raise CapabilityError(f"{instrument.value} is not subscribed on the platform")
        return self.algorithm.securities[symbol]

    # Cached fundamental reads are keyed by the catalogue they were read under.
    fundamental_catalogue_version = catalogue.VERSION

    def _supports_fundamental_field(self, name):
        return catalogue.resolve(name) is not None

    def _fundamentals(self, instrument, cutoff, fields=()):
        # The security's own record: LEAN keeps it point-in-time and attaches it to every
        # subscribed equity. It is stamped with the time LEAN read it, so a read made after
        # the Context's cutoff is refused by the shared check instead of passing as current.
        record = self._subscribed(instrument).fundamentals
        if record is None or not getattr(record, "has_fundamental_data", True):
            return None
        return _fundamental_record(instrument, record, utc(self.algorithm.utc_time, "UTC"),
                                   fields)

    def _short_availability(self, instrument, cutoff):
        security = self._subscribed(instrument)
        provider = security.shortable_provider
        # LEAN's default provider reports a zero fee and unlimited quantity for every
        # security; that is an absence of data, not free borrow, so no record is returned.
        if provider is None or type(provider).__name__ == "NullShortableProvider":
            return None
        at = self.algorithm.time
        return ShortAvailability(
            instrument,
            utc(self.algorithm.utc_time, "UTC"),
            fee_rate=_number(provider.fee_rate(security.symbol, at)),
            shortable=_number(provider.shortable_quantity(security.symbol, at)),
        )

    def _borrow(self, instrument, quantity):
        security = self.algorithm.securities[self.symbols[instrument]]
        available = security.shortable_provider.shortable_quantity(
            security.symbol, self.algorithm.time
        )
        # LEAN's IShortableProvider documents its result as "the quantity shortable
        # ... as a positive number. Null if the Symbol is shortable without
        # restrictions." So None means allowed. LEAN's default provider always
        # returns None; configure a real shortable provider on the brokerage model
        # for a backtest in which borrow can actually run out.
        if available is None:
            return Capability(True)
        # NaN or infinity is not a quantity the platform can have confirmed.
        return Capability(
            isfinite(available) and available >= quantity,
            "Platform did not confirm sufficient borrow",
        )

    def _buying_power(self, instrument, quantity):
        security = self.algorithm.securities[self.symbols[instrument]]
        return Capability(
            self.api.buying_power(self.algorithm, security, quantity),
            "LEAN initial-margin requirement exceeds available buying power",
        )

    def _cost_estimate(self, instrument, quantity, price):
        if quantity == 0:
            return 0.0
        security = self.algorithm.securities[self.symbols[instrument]]
        notional = abs(quantity * price)
        # Fee estimation queries LEAN's configured model; it never charges the account.
        fee = self.api.fee(self.algorithm, security, quantity)
        return notional * self.cost_rate + fee


class QuantConnectServices(QuantConnectDataServices):
    """LEAN data provider with fresh account, valuation, cost, and trading capabilities.

    Native subscriptions currently support US equities. Cost estimates use native
    fees and proportional slippage; actual account charges remain LEAN's responsibility.
    """

    def __init__(
        self, algorithm, symbols=None, constraints=None, api=None, cost_rate=0.0005, leverage=1.0,
        borrow_data=None,
    ):
        super().__init__(
            algorithm, symbols, constraints, api, cost_rate, leverage, account_services=True,
            borrow_data=borrow_data,
        )


def _number(value):
    """A finite float, or None for a missing or non-finite native value."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _fundamental_record(instrument, item, at, fields=()):
    """Neutral FundamentalRecord from a LEAN Fundamental object, with ``fields`` read.

    The typed fields are always read. Each declared generic field is read along its
    catalogue path and converted by its catalogue type; a missing value is None.
    """
    reference = getattr(item, "company_reference", None)
    cik = getattr(reference, "cik", None) if reference is not None else None
    cik = (str(cik).strip().lstrip("0") or None) if cik is not None else None
    sector = _read_field(item, ("asset_classification", "morningstar_sector_code"), "int")
    reit = _read_field(item, ("company_reference", "is_reit"), "bool")
    values = {}
    for name in fields:
        found = catalogue.resolve(name)
        # Installation rejected unknown names; this guards a direct call.
        if found is None:
            raise CapabilityError(f"Unsupported fundamental field: {name}")
        values[name] = _read_field(item, *found)
    return FundamentalRecord(
        instrument,
        at,
        cik=cik,
        market_cap=_number(getattr(item, "market_cap", None)) or None,
        price=_number(getattr(item, "price", None)) or None,
        dollar_volume=_number(getattr(item, "dollar_volume", None)),
        # Morningstar sector codes are three digits; LEAN gives 0 when it has none.
        morningstar_sector_code=sector or None,
        is_reit=reit,
        fields=values,
    )


def _read_field(item, path, kind):
    """One fundamental value along an attribute path, converted to its catalogue type.

    Missing is None: an absent object on the path, a non-finite number, an empty string
    or LEAN's minimum date. A real zero or False is kept. Dates (LEAN DateTime) become a
    ``date``; enumerations and other text become ``str``.
    """
    value = item
    for attr in path:
        if value is None:
            return None
        value = getattr(value, attr, None)
    if value is None:
        return None
    if kind == "float":
        return _number(value)
    if kind == "int":
        number = _number(value)
        return None if number is None else int(number)
    if kind == "bool":
        return bool(value)
    if kind == "date":
        year = getattr(value, "year", getattr(value, "Year", None))
        if year is None or int(year) <= 1:
            return None
        return date(int(year), int(getattr(value, "month", getattr(value, "Month", 1))),
                    int(getattr(value, "day", getattr(value, "Day", 1))))
    text = str(value).strip()
    return text or None
