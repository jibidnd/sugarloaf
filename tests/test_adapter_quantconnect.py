"""Adapter contract parity with minimal LEAN boundary fakes; no Cloud claim."""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from sugarloaf.adapters.memory import MemoryServices
from sugarloaf.adapters.quantconnect import QuantConnectHost, QuantConnectServices
from sugarloaf.adapters.quantconnect.host import QuantConnectGateway
from sugarloaf.adapters.quantconnect.lean import DAY, HOUR, MINUTE, LeanAPI, native_period
from sugarloaf.core.errors import CapabilityError, ContractError, NotReady, SnapshotError
from sugarloaf.core.events import DataReset, Event, EventBatch, LifecycleEvent
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.services import AccountSnapshot
from sugarloaf.core.views import PositionView
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    Instrument,
    LiquidityUniverse,
    QuotesRequirement,
    ResourceUpdate,
    Selection,
    Trigger,
    UniverseRequirement,
)
from sugarloaf.runtime import GraphRuntime

NOW = datetime(2025, 1, 2, 15, 0, tzinfo=UTC)
A = InstrumentId("USA:A")


@dataclass(frozen=True)
class Symbol:
    id: str
    value: str
    security_type: str = "Equity"


class Window:
    def __init__(self, size):
        self.size = size
        self.values = deque(maxlen=size)

    @property
    def count(self):
        return len(self.values)

    def add(self, value):
        self.values.appendleft(value)

    def __getitem__(self, index):
        return self.values[index]

    def __iter__(self):
        return iter(self.values)


class Callback:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


class Consolidator:
    def __init__(self):
        self.data_consolidated = Callback()

    def update(self, bar):
        for callback in self.data_consolidated.handlers:
            callback(self, bar)


class API:
    """Only backing platform operations are faked; neutral runtime is real."""

    def __init__(self):
        self.history_reads = 0
        self.setups = []
        self.selectors = {}
        self.submissions = []
        self.price = 100
        self.host = None
        self.disposed = []
        self.collected = []
        self.released = []
        # Period of the first subscription each symbol was given. LEAN feeds a
        # consolidator from that one, whatever is added later.
        self.feeds = {}
        self.subscriptions = []
        self.consolidators = []
        self.histories = []
        self.universes = {}

    def setup(self, algorithm, slippage, leverage, borrow_data=None):
        self.setups.append((slippage, leverage))

    def subscribe(self, algorithm, ticker, req, period=MINUTE):
        symbol = Symbol("stable:" + ticker, ticker)
        self.feeds.setdefault(symbol, period)
        self.subscriptions.append((ticker, period))
        if symbol not in algorithm.securities:
            algorithm.securities[symbol] = NS(
                symbol=symbol,
                symbol_properties=NS(lot_size=1, contract_multiplier=1),
                quote_currency=NS(symbol="USD"),
                is_tradable=True,
                price=self.price,
                holdings=NS(quantity=0),
                exchange=NS(hours=NS(is_open=lambda *_: True)),
                shortable_provider=NS(shortable_quantity=lambda *_: 10),
            )
        return symbol

    def window(self, count):
        return Window(count)

    def feed_period(self, algorithm, symbol, quotes=False):
        return self.feeds.get(symbol)

    def consolidator(self, req, security, period=MINUTE):
        self.consolidators.append((security.symbol.value, req.interval, period))
        return Consolidator()

    def scan(self, consolidator, security, cutoff):
        if hasattr(consolidator, "scan"):
            consolidator.scan(cutoff)

    def history(self, algorithm, symbol, req, cutoff, count, period=MINUTE):
        self.history_reads += 1
        self.histories.append((symbol.value, req.interval, period))
        if isinstance(req, QuotesRequirement):
            return [
                NS(
                    time=cutoff - req.interval,
                    end_time=cutoff,
                    bid=NS(close=99),
                    ask=NS(close=101),
                    last_bid_size=50,
                    last_ask_size=40,
                )
            ]
        return [
            NS(
                time=cutoff - req.interval * (i + 1),
                end_time=cutoff - req.interval * i,
                open=self.price,
                high=self.price + 1,
                low=self.price - 1,
                close=self.price,
                volume=1000,
                is_fill_forward=False,
            )
            for i in reversed(range(count))
        ]

    def universe(self, algorithm, definition, selector, etf_ticker=None, feed=None, period=DAY):
        self.selectors[definition.name] = selector
        self.universes[definition.name] = (feed, period)
        return NS(name=definition.name, selected=[], members={})

    def dispose_universe(self, universe):
        self.disposed.append(universe)

    def collect_universe(self, algorithm, universe):
        if universe.selected or universe.members:
            return False
        self.collected.append(universe)
        return True

    def release_subscription(self, algorithm, symbol):
        if algorithm.securities[symbol].holdings.quantity:
            return False
        self.released.append(symbol)
        return True

    def unchanged(self):
        return "UNCHANGED"

    def buying_power(self, *args):
        return True

    def fee(self, algorithm, security, quantity):
        return max(1.0, abs(quantity) * 0.005)

    def to_utc(self, security, value):
        return value.astimezone(UTC)

    def is_open(self, security, at, extended):
        return security.exchange.hours.is_open(at, extended)

    def session_close(self, security, after, count):
        # Stand-in calendar: one regular session closes each day.
        return after + timedelta(days=count)

    def refusal(self, ticket):
        return getattr(ticket, "refused", None)

    def submit(self, algorithm, symbol, intent):
        self.submissions.append(intent)
        order_id = len(self.submissions)
        ticket = NS(order_id=order_id, quantity_filled=intent.quantity)
        algorithm.transactions.tickets[order_id] = ticket
        algorithm.transactions.orders[order_id] = NS(tag=intent.intent_id)
        algorithm.securities[symbol].holdings.quantity += intent.quantity
        if self.host:
            # Exercise an actual callback while gateway.submit is still on the stack.
            self.host.on_order_event(
                NS(order_id=order_id, status="Filled", utc_time=NOW, message="")
            )
        return ticket


def algorithm():
    tickets, orders = {}, {}
    return NS(
        utc_time=NOW,
        time=NOW,
        is_warming_up=False,
        securities={},
        account_currency="USD",
        portfolio=NS(total_portfolio_value=10000, cash_book={"USD": NS(amount=10000)}),
        subscription_manager=NS(
            add_consolidator=lambda *_: None, remove_consolidator=lambda *_: None
        ),
        transactions=NS(
            tickets=tickets,
            orders=orders,
            get_open_orders=lambda: [],
            get_order_ticket=lambda i: tickets[i],
            get_order_by_id=lambda i: orders[i],
            cancel_order=lambda *_: None,
        ),
    )


def slice_data(platform, **fields):
    return NS(
        bars={
            symbol: native_bar(platform.utc_time, security.price)
            for symbol, security in platform.securities.items()
        },
        **fields,
    )


class Signal:
    inputs = {}
    outputs = {"positions": OutputSpec((PositionView,))}
    triggers = (Trigger("data"),)

    def __init__(self, req):
        self.requirements = (req,)
        self.reads = []

    def evaluate(self, inputs, ctx):
        prices = tuple(b.close for b in ctx.market.bars(self.requirements[0], A))
        self.reads.append(prices)
        return NodeOutputs(
            {
                "positions": (
                    PositionView(
                        {A: 2},
                        "quantity",
                        "account",
                        Metadata(
                            f"decision:{ctx.snapshot_id}",
                            ctx.node_id,
                            ctx.now,
                            ctx.now + timedelta(days=1),
                        ),
                    ),
                )
            }
        )


def compiled(node):
    return Graph().add_node("signal", node).set_output("signal", "positions").compile()


def test_real_runtime_memory_qc_parity_submission_callback_and_replay():
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1), 2)
    api, platform = API(), algorithm()
    signal = Signal(req)
    host = QuantConnectHost(platform, compiled(signal), tickers={A: "A"}, api=api)
    api.host = host
    assert api.setups == [(0.0005, 1.0)]
    assert "account" not in host.services.read_counts
    host.on_data(slice_data(platform))
    assert len(api.submissions) == 1
    assert next(iter(host.execution.state.intents.values())).status == "filled"
    host.on_data(slice_data(platform))
    assert len(api.submissions) == 1  # Filled total holdings already satisfy replacement target.

    memory = MemoryServices(
        [Instrument(A, "A")], AccountSnapshot({}, 10000, {"USD": 10000}, {A: 100})
    )
    memory.put_bars(
        req,
        [
            Bar(
                A,
                NOW - timedelta(minutes=2),
                NOW - timedelta(minutes=1),
                NOW - timedelta(minutes=1),
                100,
                101,
                99,
                100,
                1000,
            ),
            Bar(A, NOW - timedelta(minutes=1), NOW, NOW, 100, 101, 99, 100, 1000),
        ],
    )
    memory_signal = Signal(req)
    runtime = GraphRuntime(compiled(memory_signal), memory)
    runtime.process(EventBatch(1, NOW, (Event("data"),)))
    assert memory_signal.reads == signal.reads[:1]
    assert runtime.store.get("signal", "positions")[0].positions == (
        host.runtime.store.get("signal", "positions")[0].positions
    )


def test_qc_shared_mixed_resources_lazy_snapshot_and_split_rewarm():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    # Raw prices: the one convention this adapter rebuilds on a split.
    minute = BarsRequirement(Selection((A,)), timedelta(minutes=1), 2, normalization="raw")
    hour = BarsRequirement(Selection((A,)), timedelta(hours=1), 2, normalization="raw")
    for node in ("n", "m"):
        services.install(node, ResourceUpdate((minute, hour), ()))
    services.apply(EventBatch(0, NOW))
    assert len(services.windows) == api.history_reads == 2
    first = services.context(node_id="n")
    second = services.context(node_id="m")
    assert first.market.bars(minute, A) == second.market.bars(minute, A)
    assert list(services.read_counts.values()) == [1]
    reader = first.market
    services._changed()
    with pytest.raises(SnapshotError):
        reader.bars(minute, A)
    api.price = 50
    discovered = services.apply(
        EventBatch(
            1, NOW, (Event("lifecycle", A, payload=LifecycleEvent("split", A, NOW, factor=0.5)),)
        )
    )
    assert api.history_reads == 4
    # Both windows were rebuilt, and the adapter reports once, naming every node
    # that had declared them, so the runtime can withdraw what they published.
    assert [event.payload for event in discovered if event.kind == "data_reset"] == [
        DataReset(A, ("m", "n"), NOW)
    ]
    assert all(b.close == 50 for b in services.context(node_id="n").market.bars(minute, A))
    renamed = Symbol("stable:A", "RENAMED")
    assert services.bind(renamed) == A


def test_qc_universe_selection_and_definition_replacement():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    definition = LiquidityUniverse("liquid", 1)
    services.install("n", ResourceUpdate((UniverseRequirement(definition),), ()))
    a, b = Symbol("a", "A"), Symbol("b", "B")
    result = api.selectors["liquid"](
        [
            NS(symbol=b, price=100, volume=10, dollar_volume=1000),
            NS(symbol=a, price=100, volume=10, dollar_volume=1000),
        ]
    )
    assert result == [a]
    assert api.selectors["liquid"]([]) == "UNCHANGED"
    platform.utc_time += timedelta(days=1)
    assert api.selectors["liquid"]([]) == []
    assert services.memberships["liquid"].members == ()
    original = services.universes["liquid"]
    old_selector = api.selectors["liquid"]
    services.install(
        "n", ResourceUpdate((UniverseRequirement(LiquidityUniverse("liquid", 3)),), ())
    )
    assert api.disposed == [original]
    assert old_selector([]) == []
    assert "liquid" not in services.memberships
    assert api.selectors["liquid"](
        [
            NS(symbol=b, price=100, volume=10, dollar_volume=1000),
            NS(symbol=a, price=100, volume=10, dollar_volume=1000),
        ]
    ) == [a, b]


def test_universe_owner_release_defers_collection_and_preserves_held_members():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    definition = UniverseRequirement(LiquidityUniverse("u", 1))
    services.install("owner", ResourceUpdate((definition,), ()))
    services.install("consumer", ResourceUpdate((BarsRequirement(Selection(universe="u")),), ()))
    original = services.universes["u"]
    original.members["held"] = NS(quantity=100)
    services.install("owner", ResourceUpdate((), ()))
    assert services.universes["u"] is original
    services.install("consumer", ResourceUpdate((), ()))
    assert not services.universes and api.disposed == [original]
    services.apply(EventBatch(0, NOW))
    services.apply(EventBatch(1, NOW))
    assert api.collected == []  # Platform-retained holdings are still discoverable.
    original.members.clear()  # LEAN confirms the old source has no retained members.
    services.apply(EventBatch(2, NOW))
    assert api.collected == [original]
    assert services._retired_universes == []


def test_universe_refresh_rejects_unimplemented_dimensions():
    services = QuantConnectServices(algorithm(), api=API())
    for refresh in (
        Trigger("scheduled", key="daily"),
        Trigger("universe", key="x"),
        Trigger("universe", interval=timedelta(hours=1)),
    ):
        with pytest.raises(CapabilityError, match="refresh"):
            services.install(
                "n",
                ResourceUpdate(
                    (UniverseRequirement(LiquidityUniverse("u", 1, refresh=refresh)),), ()
                ),
            )



def test_manual_data_ownership_released_only_after_last_consumer_without_liquidation():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    for node in ("a", "b"):
        services.install(node, ResourceUpdate((req,), ()))
    services.apply(EventBatch(0, NOW))
    symbol = services.symbols[A]
    platform.securities[symbol].holdings.quantity = 100
    services.install("a", ResourceUpdate((), ()))
    services.apply(EventBatch(1, NOW))
    assert api.released == []
    services.install("b", ResourceUpdate((), ()))
    services.apply(EventBatch(2, NOW))
    assert api.released == []
    assert platform.securities[symbol].holdings.quantity == 100
    platform.securities[symbol].holdings.quantity = 0
    services.apply(EventBatch(3, NOW))
    assert api.released == [symbol]


def test_qc_fee_estimate_uses_exact_platform_model_and_preserves_no_trade_zero():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    services.install("n", ResourceUpdate((BarsRequirement(Selection((A,))),), ()))
    services.apply(EventBatch(0, NOW))
    costs = services.context().costs
    assert costs.estimate(A, 0, 100) == 0
    assert costs.estimate(A, 1, 100) == pytest.approx(0.05 + 1)
    assert costs.estimate(A, 10, 100) == pytest.approx(0.5 + 1)
    assert costs.estimate(A, -1000, 100) == pytest.approx(50 + 5)


def test_quote_trade_distinction_multiple_intervals_and_semantic_conflicts():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    requests = tuple(
        BarsRequirement(Selection((A,)), interval)
        for interval in (
            timedelta(minutes=1),
            timedelta(minutes=5),
            timedelta(hours=1),
            timedelta(days=1),
        )
    )
    quote = QuotesRequirement(Selection((A,)), timedelta(minutes=5))
    services.install("n", ResourceUpdate(requests + (quote,), ()))
    services.apply(EventBatch(0, NOW))
    ctx = services.context(node_id="n")
    assert len(services.windows) == 5
    assert ctx.market.quotes(quote, A)[0].bid == 99
    assert ctx.market.quotes(quote, A)[0].ask == 101
    assert ctx.market.bars(requests[1], A)[0].close == 100
    incompatible = BarsRequirement(Selection((A,)), normalization="raw")
    services.install("m", ResourceUpdate((incompatible,), ()))
    with pytest.raises(CapabilityError, match="Incompatible"):
        services.apply(EventBatch(1, NOW))


def test_daily_consolidation_uses_actual_early_close_session():
    api = object.__new__(LeanAPI)
    api.lean = NS(
        TradeBarConsolidator=lambda calendar: calendar,
        CalendarInfo=lambda start, period: (start, period),
    )
    opening = datetime(2025, 11, 28, 9, 30)
    closing = datetime(2025, 11, 28, 13)
    security = NS(
        exchange=NS(
            hours=NS(
                get_next_market_open=lambda midnight, extended: opening,
                get_next_market_close=lambda start, extended: closing,
            )
        )
    )
    calendar = api.consolidator(BarsRequirement(Selection((A,))), security)
    assert calendar(opening) == (opening, timedelta(hours=3, minutes=30))


def test_delisting_blocks_opening_and_non_equity_metadata_is_rejected():
    api, platform = API(), algorithm()
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    host = QuantConnectHost(platform, compiled(Signal(req)), tickers={A: "A"}, api=api)
    symbol = host.services.symbols[A]
    host.on_data(slice_data(platform, delistings={symbol: NS()}))
    assert api.submissions == []
    option = Symbol("option", "A CALL", "Option")
    instrument = host.services.bind(option)
    platform.securities[option] = platform.securities[symbol]
    with pytest.raises(CapabilityError, match="US equities"):
        host.services.context().instruments.get(instrument)


def test_multi_instrument_synchronous_fills_are_drained_before_reconciliation():
    b = InstrumentId("USA:B")
    req = BarsRequirement(Selection((A, b)), timedelta(minutes=1))

    class TwoSignal(Signal):
        def evaluate(self, inputs, ctx):
            return NodeOutputs(
                {
                    "positions": (
                        PositionView(
                            {A: 20, b: 30},
                            "quantity",
                            "account",
                            Metadata(
                                f"decision:{ctx.snapshot_id}",
                                ctx.node_id,
                                ctx.now,
                                ctx.now + timedelta(days=1),
                            ),
                        ),
                    )
                }
            )

    class SynchronousAPI(API):
        def submit(self, algorithm, symbol, intent):
            assert len(self.submissions) < 4, "Reconciliation created a phantom round trip"
            # LEAN registers the ticket, then can deliver Submitted and Filled before
            # MarketOrder returns. The host passes each event on as it arrives.
            order_id = len(self.submissions) + 1
            algorithm.transactions.tickets[order_id] = NS(order_id=order_id, quantity_filled=0)
            algorithm.transactions.orders[order_id] = NS(tag=intent.intent_id)
            self.host.on_order_event(
                NS(order_id=order_id, status="Submitted", utc_time=NOW, message="")
            )
            return super().submit(algorithm, symbol, intent)

    api, platform = SynchronousAPI(), algorithm()
    host = QuantConnectHost(platform, compiled(TwoSignal(req)), tickers={A: "A", b: "B"}, api=api)
    api.host = host
    host.on_data(slice_data(platform))
    assert [(i.instrument, i.quantity) for i in api.submissions] == [(A, 20), (b, 30)]
    assert all(i.status == "filled" for i in host.execution.state.intents.values())
    assert platform.securities[host.services.symbols[A]].holdings.quantity == 20
    assert platform.securities[host.services.symbols[b]].holdings.quantity == 30
    host.on_data(slice_data(platform))
    assert len(api.submissions) == 2


class NativeIndicator:
    """Observable native boundary fake; no portable EMA formula implementation."""

    def __init__(self, size):
        self.window = Window(size)
        self.warm_up_period = 2
        self.samples = 0
        self.is_ready = False


class IndicatorAPI(API):
    def __init__(self):
        super().__init__()
        self.updates = []
        self.ranges = []

    def indicator(self, requirement):
        native = NativeIndicator(requirement.window)
        return native, {"value": native}, "close"

    def update_indicator(self, indicator, kind, field, bar):
        self.updates.append((indicator, bar.end_time))
        indicator.samples += 1
        indicator.is_ready = indicator.samples >= indicator.warm_up_period
        indicator.window.add(NS(end_time=bar.end_time, value=bar.close))

    def indicator_version(self):
        return "native-test-boundary"

    def corporate_actions(self, algorithm, symbol, start, end):
        return ()

    def history_range(self, algorithm, symbol, source, start, end):
        self.ranges.append((start, end, source.normalization))
        for index in range(1, 4):
            at = NOW + timedelta(minutes=index)
            if start < at <= end:
                yield native_bar(at, 100 + index)


def native_bar(end, price):
    return NS(
        time=end - timedelta(minutes=1),
        end_time=end,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1000,
        is_fill_forward=False,
    )


def test_native_indicator_shared_window_prefill_admission_and_late_input():
    from sugarloaf.market.contracts import IndicatorRequirement

    api, platform = IndicatorAPI(), algorithm()
    provider = QuantConnectServices(platform, api=api)
    provider.register(A, "A")
    source = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    indicator = IndicatorRequirement(source, "ema", (("period", 2),), window=3)
    raw = BarsRequirement(Selection((A,)), timedelta(minutes=1), lookback=3)
    provider.install("a", ResourceUpdate((indicator, raw), ()))
    provider.install("b", ResourceUpdate((indicator,), ()))
    provider.apply(EventBatch(0, NOW))
    assert len(provider.native_indicators) == 1
    assert len(api.updates) == 4  # warm-up 2 + three valid outputs - one
    snapshot = provider.context(node_id="a").market.indicator(indicator, A)
    assert snapshot.ready and len(snapshot.points) == 3
    assert not provider.pending_events  # Prefill never emits historical decisions.
    prior = provider.context(node_id="a")
    platform.utc_time = NOW + timedelta(minutes=1, seconds=2)
    bar = native_bar(NOW + timedelta(minutes=1), 120)
    next(iter(provider.consolidators.values())).update(bar)
    with pytest.raises(SnapshotError):
        prior.market.indicator(indicator, A)
    # Queued native inputs are not visible until the next admission boundary.
    assert provider.context(node_id="a").market.indicator(indicator, A).points[-1].time == NOW
    provider.apply(EventBatch(1, bar.end_time, received_at=platform.utc_time))
    ctx = provider.context(node_id="a")
    assert ctx.market.indicator(indicator, A).points[-1].values["value"] == 120
    assert ctx.market.bars(raw, A)[-1].available_at == platform.utc_time
    updates = len(api.updates)
    next(iter(provider.consolidators.values())).update(bar)
    provider.apply(EventBatch(2, bar.end_time, received_at=platform.utc_time))
    assert len(api.updates) == updates  # history/live overlap cannot doublefeed
    late = native_bar(NOW + timedelta(seconds=30), 110)
    next(iter(provider.consolidators.values())).update(late)
    provider.apply(EventBatch(3, bar.end_time, received_at=platform.utc_time))
    ctx = provider.context(node_id="a")
    assert len(api.updates) == updates
    assert provider.indicator_diagnostics[-1]["code"] == "NATIVE_INDICATOR_LATE_INPUT"
    assert any(b.end == late.end_time for b in ctx.market.bars(raw, A))
    provider.install("a", ResourceUpdate((), ()))
    provider.apply(EventBatch(4, bar.end_time, received_at=platform.utc_time))
    assert len(provider.native_indicators) == 1
    provider.install("b", ResourceUpdate((), ()))
    provider.apply(EventBatch(5, bar.end_time, received_at=platform.utc_time))
    assert not provider.native_indicators


def test_quantbook_replay_admits_one_batch_without_account_or_future_indicators():
    from sugarloaf.adapters.quantconnect import QuantBookServices
    from sugarloaf.market.contracts import IndicatorRequirement

    api, platform = IndicatorAPI(), algorithm()
    provider = QuantBookServices(platform, tickers={A: "A"}, api=api)
    source = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    indicator = IndicatorRequirement(source, "ema", (("period", 2),), window=2)

    class Probe(Signal):
        def __init__(self):
            super().__init__(source)
            self.requirements = (source, indicator)
            self.seen = []

        def evaluate(self, inputs, ctx):
            self.seen.append((ctx.now, ctx.market.indicator(indicator, A).points[-1].time))
            with pytest.raises(CapabilityError, match="unavailable"):
                _ = ctx.portfolio
            return super().evaluate(inputs, ctx)

    probe = Probe()
    runtime = GraphRuntime(compiled(probe), provider)
    end = NOW + timedelta(minutes=3)
    provider.prepare(NOW, end)
    assert api.setups == [] and probe.seen == []
    batches = provider.history_batches(NOW, end)
    first = next(batches)
    assert len(api.updates) == 3  # preparation only, staged first sample not yet admitted
    runtime.process(first)
    for batch in batches:
        runtime.process(batch)
    assert len(probe.seen) == 3
    assert all(clock == indicator_time for clock, indicator_time in probe.seen)
    assert probe.reads == [(101.0,), (102.0,), (103.0,)]
    snapshot = provider.context(node_id="signal").market.indicator(indicator, A)
    assert snapshot.provenance["arrival_clock"] == "assumed_history"
    anchor = provider.anchor_observation(A, end, "price_return", end)
    assert anchor.value == 103
    assert provider.anchor_observation(A, end, "price_return", NOW) is None
    assert provider.context(node_id="signal").market.indicator(indicator, A) == snapshot


def test_empty_host_callbacks_do_not_start_or_advance_graph_clock():
    api, platform = API(), algorithm()
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    host = QuantConnectHost(platform, compiled(Signal(req)), tickers={A: "A"}, api=api)
    host.on_data(NS())
    assert host.sequence == 0 and host.runtime.batch is None
    host.on_data(slice_data(platform))
    assert host.sequence == 1 and host.runtime.batch.cutoff == NOW
    platform.utc_time += timedelta(days=1)
    host.on_data(NS())
    assert host.sequence == 1 and host.runtime.batch.cutoff == NOW


def test_queued_callback_facts_wait_for_a_data_boundary_past_a_lifecycle_only_slice():
    # Raw prices, so the dividend below does not itself re-evaluate the node.
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1), normalization="raw")

    class Scheduled(Signal):
        # Evaluated only when the externally scheduled fact "rebalance" is delivered.
        triggers = (Trigger("scheduled", key="rebalance"),)

    api, platform = API(), algorithm()
    signal = Scheduled(req)
    host = QuantConnectHost(platform, compiled(signal), tickers={A: "A"}, api=api)
    api.host = host
    host.on_data(slice_data(platform))  # Establish the graph clock with real data.
    assert signal.reads == []
    host.schedule("rebalance")
    # LEAN delivers a slice holding only a corporate action, with no bars. The
    # clock cannot advance, so the queued fact must not be spent on this callback.
    symbol = host.services.symbols[A]
    host.on_data(NS(dividends={symbol: NS(distribution=1.0)}))
    assert signal.reads == []
    assert [event.kind for event in host._held] == ["scheduled"]
    # The next slice with bars is the data boundary the fact was queued for.
    platform.utc_time += timedelta(minutes=1)
    host.on_data(slice_data(platform))
    assert len(signal.reads) == 1
    assert not host._held and not host.services.pending_events


def test_quantbook_scans_native_consolidation_at_each_admitted_boundary():
    from sugarloaf.adapters.quantconnect import QuantBookServices
    from sugarloaf.market.contracts import IndicatorRequirement

    class PeriodConsolidator(Consolidator):
        def __init__(self, interval):
            super().__init__()
            self.interval, self.working = interval, []

        def scan(self, at):
            if self.working and at >= self.working[0].time + self.interval:
                first, last = self.working[0], self.working[-1]
                result = native_bar(first.time + self.interval, last.close)
                result.time = first.time
                self.working = []
                for callback in self.data_consolidated.handlers:
                    callback(self, result)

        def update(self, bar):
            # Like LEAN, Update compares input START, not input end.
            self.scan(bar.time)
            self.working.append(bar)

    class PeriodAPI(IndicatorAPI):
        def consolidator(self, req, security, period=MINUTE):
            return PeriodConsolidator(req.interval)

        def history(self, algorithm, symbol, req, cutoff, count, period=MINUTE):
            for i in reversed(range(count * 5)):
                yield native_bar(cutoff - timedelta(minutes=i), 100)

        def history_range(self, algorithm, symbol, source, start, end):
            for i in range(1, 11):
                at = NOW + timedelta(minutes=i)
                if start < at <= end:
                    yield native_bar(at, 100 + i)

    source = BarsRequirement(Selection((A,)), timedelta(minutes=5))
    indicator = IndicatorRequirement(source, "ema", (("period", 2),))

    class Probe(Signal):
        def __init__(self):
            super().__init__(source)
            self.requirements = (source, indicator)
            self.triggers = (Trigger("bar_close", interval=source.interval),)
            self.seen = []

        def evaluate(self, inputs, ctx):
            self.seen.append((ctx.now, ctx.market.indicator(indicator, A).points[-1].time))
            return super().evaluate(inputs, ctx)

    provider = QuantBookServices(algorithm(), tickers={A: "A"}, api=PeriodAPI())
    probe = Probe()
    runtime = GraphRuntime(compiled(probe), provider)
    end = NOW + timedelta(minutes=10)
    provider.prepare(NOW, end)
    entry = next(iter(provider.native_indicators.values()))
    assert entry["last"] == NOW and entry["native"].is_ready
    for batch in provider.history_batches(NOW, end):
        runtime.process(batch)
    assert probe.seen == [(NOW + timedelta(minutes=i), NOW + timedelta(minutes=i)) for i in (5, 10)]
    assert probe.reads == [(105.0,), (110.0,)]


@pytest.mark.parametrize("calendar_only", [False, True])
def test_quantbook_schedule_ticks_on_the_minute_feed_a_node_or_its_calendar_asks_for(
    calendar_only,
):
    # A schedule is checked when data arrives. A node that must act inside the
    # session therefore declares minute data (or a session calendar, which owns a
    # minute feed); the daily bars it also reads do not move its clock.
    from sugarloaf.adapters.base import ProviderServices
    from sugarloaf.adapters.quantconnect import QuantBookServices
    from sugarloaf.market.contracts import Schedule

    class DailyConsolidator(Consolidator):
        def update(self, bar):
            pass  # No new daily close during this intraday replay interval.

    class ScheduledAPI(IndicatorAPI):
        def consolidator(self, req, security, period=MINUTE):
            return DailyConsolidator() if req.interval == timedelta(days=1) else Consolidator()

        def history_range(self, algorithm, symbol, source, start, end):
            assert source.interval == timedelta(minutes=1)
            return super().history_range(algorithm, symbol, source, start, end)

        def schedule_due(self, algorithm, symbols, schedule, after, through):
            # Calendar membership is asserted separately; this fixture's UTC session is open.
            if schedule.calendar:
                assert schedule.calendar in symbols
                schedule = Schedule(at=schedule.at)
            return ProviderServices().schedule_due(schedule, after, through)

    source = BarsRequirement(Selection((A,)))
    clock = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    at = (NOW + timedelta(minutes=2)).time().replace(tzinfo=None)

    class ScheduledSignal(Signal):
        def __init__(self):
            super().__init__(source)
            self.requirements = () if calendar_only else (source, clock)
            self.triggers = (
                Trigger(
                    "scheduled", schedule=Schedule(at=at, calendar=A if calendar_only else None)
                ),
            )
            self.times = []

        def evaluate(self, inputs, ctx):
            self.times.append(ctx.now)
            if not calendar_only:
                assert ctx.market.bars(source, A)[-1].end == NOW
                return super().evaluate(inputs, ctx)
            return NodeOutputs({"positions": ()})

    signal = ScheduledSignal()
    provider = QuantBookServices(algorithm(), tickers={A: "A"}, api=ScheduledAPI())
    runtime = GraphRuntime(compiled(signal), provider)
    end = NOW + timedelta(minutes=3)
    provider.prepare(NOW, end)
    for batch in provider.history_batches(NOW, end):
        runtime.process(batch)
    assert signal.times == [NOW + timedelta(minutes=2)]


def test_quantbook_rejects_unrecognized_corporate_action_before_decisions():
    from sugarloaf.adapters.quantconnect import QuantBookServices

    class ActionsAPI(IndicatorAPI):
        def corporate_actions(self, algorithm, symbol, start, end):
            return (NS(end_time=NOW + timedelta(minutes=1)),)

    provider = QuantBookServices(algorithm(), tickers={A: "A"}, api=ActionsAPI())
    signal = Signal(BarsRequirement(Selection((A,)), timedelta(minutes=1)))
    GraphRuntime(compiled(signal), provider)
    with pytest.raises(CapabilityError, match="Unsupported typed research corporate action"):
        provider.prepare(NOW, NOW + timedelta(minutes=3))
    assert signal.reads == []


def test_same_timestamp_quote_revision_preserves_changed_sizes():
    api, platform = API(), algorithm()
    provider = QuantConnectServices(platform, api=api)
    provider.register(A, "A")
    request = QuotesRequirement(Selection((A,)))
    provider.install("quotes", ResourceUpdate((request,), ()))
    provider.apply(EventBatch(0, NOW))
    revision = NS(
        time=NOW - timedelta(minutes=1),
        end_time=NOW,
        bid=NS(close=99),
        ask=NS(close=101),
        last_bid_size=90,
        last_ask_size=80,
    )
    next(iter(provider.consolidators.values())).update(revision)
    provider.apply(EventBatch(1, NOW))
    quote = provider.context(node_id="quotes").market.quotes(request, A)[-1]
    assert (quote.bid_size, quote.ask_size) == (90, 80)


def test_qc_cancel_request_response_distinguishes_pending_accepted_and_rejected():
    platform = algorithm()
    services = QuantConnectServices(platform, api=API())
    gateway = QuantConnectGateway(services)
    response = NS(is_success=False, is_error=False)
    ticket = NS(get_most_recent_order_response=lambda: response)
    platform.transactions.tickets[1] = ticket
    requested = []

    def cancel(order_id, tag):
        requested.append((order_id, tag))
        return ticket

    platform.transactions.cancel_order = cancel
    version = services.version
    gateway.cancel("1")
    assert services.version == version + 1
    assert gateway.cancel_status("1") is None
    response = NS(is_success=True, is_error=False)
    assert gateway.cancel_status("1").supported
    response = NS(
        is_success=False, is_error=True, error_code="BrokerageFailedToCancelOrder",
        error_message="Temporary brokerage rejection",
    )
    outcome = gateway.cancel_status("1")
    assert not outcome.supported and "Temporary brokerage rejection" in outcome.reason
    with pytest.raises(NotReady, match="QC_CANCEL_REJECTED.*Temporary brokerage rejection"):
        gateway.cancel("1")
    assert len(requested) == 2
    assert services.version == version + 2


def test_qc_cancel_without_returned_ticket_fails_explicitly():
    platform = algorithm()
    services = QuantConnectServices(platform, api=API())
    gateway = QuantConnectGateway(services)
    # A missing ticket provides no confirmation that a request was submitted.
    with pytest.raises(ContractError, match="no platform order ticket"):
        gateway.cancel("1")


def test_qc_order_status_mapping_surfaces_unknown_platform_state():
    api, platform = API(), algorithm()
    request = BarsRequirement(Selection((A,)), timedelta(minutes=1))
    host = QuantConnectHost(platform, compiled(Signal(request)), tickers={A: "A"}, api=api)
    api.host = host
    host.on_data(slice_data(platform))
    event = NS(order_id=1, status="UpdateSubmitted", utc_time=NOW, message="")
    update = host._order_update(event)
    assert update.status == "open" and update.filled == 2
    event.status = "UnexpectedBrokerState"
    with pytest.raises(ContractError, match="Unsupported QC order status"):
        host._order_update(event)


@pytest.mark.parametrize("available", [None, float("inf"), float("nan"), 1, 10])
def test_qc_borrow_follows_the_platform_shortable_contract(available):
    # LEAN documents None as "shortable without restrictions"; a number is a limit.
    platform = algorithm()
    services = QuantConnectServices(platform, api=API())
    services.register(A, "A")
    request = BarsRequirement(Selection((A,)))
    services.install("signal", ResourceUpdate((request,), ()))
    services.apply(EventBatch(0, NOW))
    security = platform.securities[services.symbols[A]]
    security.shortable_provider.shortable_quantity = lambda *_: available
    assert services.context().portfolio.borrow(A, 2).supported == (available in (None, 10))


def test_only_a_split_on_raw_prices_rebuilds_and_reports_a_data_reset():
    b = InstrumentId("USA:B")
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    services.register(b, "B")
    raw = BarsRequirement(Selection((A,)), timedelta(minutes=1), normalization="raw")
    adjusted = BarsRequirement(Selection((b,)), timedelta(minutes=1), normalization="adjusted")
    services.install("n", ResourceUpdate((raw, adjusted), ()))
    services.apply(EventBatch(0, NOW))
    assert api.history_reads == 2  # One warmup history request per window.

    def resets(sequence, kind, instrument, **fields):
        """Apply one corporate action and return the data resets the adapter reports."""
        event = LifecycleEvent(kind, instrument, NOW, **fields)
        batch = EventBatch(sequence, NOW, (Event("lifecycle", instrument, payload=event),))
        return [e.payload for e in services.apply(batch) if e.kind == "data_reset"]

    # A dividend rebuilds nothing under either convention. LEAN's adjusted series
    # is continuous through the ex-date from its first bar, and a raw price that
    # opens lower by the dividend is a real price move.
    assert resets(1, "dividend", A, amount=1.0) == []
    assert resets(2, "dividend", b, amount=1.0) == []
    # A split leaves the adjusted series continuous as well.
    assert resets(3, "split", b, factor=0.5) == []
    assert api.history_reads == 2
    # Only raw prices change scale on a split: that window is re-read and reported.
    assert resets(4, "split", A, factor=0.5) == [DataReset(A, ("n",), NOW)]
    assert api.history_reads == 3


def test_window_keeps_first_receipt_for_repeats_and_orders_late_arrivals():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    request = BarsRequirement(Selection((A,)), timedelta(minutes=1), lookback=3)
    services.install("n", ResourceUpdate((request,), ()))
    services.apply(EventBatch(0, NOW))
    consolidator = next(iter(services.consolidators.values()))

    def admit(bar, received):
        platform.utc_time = received
        consolidator.update(bar)
        services.apply(EventBatch(services.batch.sequence + 1, NOW, received_at=received))
        return services.context(node_id="n").market.bars(request, A)

    later = NOW + timedelta(minutes=5)
    # An identical repeat of the newest bar keeps the receipt time it first had.
    bars = admit(native_bar(NOW, 100), later)
    assert bars[-1].available_at == NOW
    # A revision of that bar carries the receipt time of the revised content.
    bars = admit(native_bar(NOW, 105), later)
    assert (bars[-1].close, bars[-1].available_at) == (105, later)
    # A late older bar is slotted in chronologically and cannot evict newer bars.
    late = native_bar(NOW - timedelta(seconds=90), 90)
    bars = admit(late, later + timedelta(minutes=1))
    assert [bar.end for bar in bars] == sorted(bar.end for bar in bars)
    assert bars[-1].close == 105 and any(bar.close == 90 for bar in bars)


def test_session_close_steps_whole_regular_sessions_through_the_native_calendar():
    closes = [datetime(2025, 1, 10, 16), datetime(2025, 1, 13, 16), datetime(2025, 1, 14, 16)]
    requests = []

    def get_next_market_close(local, extended):
        # Like LEAN: the search is non-inclusive of the instant it is given.
        requests.append(extended)
        return next(close for close in closes if close > local)

    api = object.__new__(LeanAPI)
    # This fixture's exchange zone is UTC, so the conversions are identities.
    api.lean = NS(
        Extensions=NS(
            convert_from_utc=lambda value, zone: value, convert_to_utc=lambda value, zone: value
        )
    )
    security = NS(
        exchange=NS(time_zone="UTC", hours=NS(get_next_market_close=get_next_market_close))
    )
    friday_close = datetime(2025, 1, 10, 16, tzinfo=UTC)
    # Asked at Friday's close, one session later is Monday's close, not Friday's.
    assert api.session_close(security, friday_close, 1) == datetime(2025, 1, 13, 16, tzinfo=UTC)
    assert api.session_close(security, friday_close, 2) == datetime(2025, 1, 14, 16, tzinfo=UTC)
    assert requests == [False] * 3  # Regular sessions only, never extended hours.


@pytest.mark.parametrize(
    ("req", "period"),
    [
        (BarsRequirement(Selection((A,))), DAY),
        (BarsRequirement(Selection((A,)), timedelta(hours=1)), HOUR),
        (BarsRequirement(Selection((A,)), timedelta(hours=2)), HOUR),
        (BarsRequirement(Selection((A,)), timedelta(minutes=90)), MINUTE),
        (BarsRequirement(Selection((A,)), timedelta(minutes=5)), MINUTE),
        # Native hourly and daily bars cover the regular session only.
        (BarsRequirement(Selection((A,)), session="extended"), MINUTE),
        # LEAN has no hourly or daily quote bars for equities.
        (QuotesRequirement(Selection((A,)), timedelta(hours=1)), MINUTE),
    ],
)
def test_native_period_is_the_coarsest_native_feed_that_divides_the_interval(req, period):
    assert native_period(req) == period


def feeds(*requirements, triggers=()):
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    services.register(B, "B")
    services.install("n", ResourceUpdate(tuple(requirements), tuple(triggers)))
    services.apply(EventBatch(0, NOW))
    return api, services


B = InstrumentId("USA:B")


def test_a_node_that_reads_daily_bars_gets_a_daily_feed_passed_through_unchanged():
    api, _ = feeds(BarsRequirement(Selection((A,)), lookback=3))
    assert api.subscriptions == [("A", DAY)]
    # Interval equal to the feed period: the native identity path, no rebuilding.
    assert api.consolidators == [("A", DAY, DAY)]
    assert api.histories == [("A", DAY, DAY)]


def test_the_finest_reader_sets_the_feed_and_coarser_intervals_are_built_from_it():
    api, _ = feeds(
        BarsRequirement(Selection((A,))),
        BarsRequirement(Selection((A,)), timedelta(minutes=5)),
        BarsRequirement(Selection((B,)), timedelta(hours=1)),
        BarsRequirement(Selection((B,)), timedelta(hours=2)),
    )
    assert sorted(api.subscriptions) == [("A", MINUTE), ("B", HOUR)]
    assert sorted(api.consolidators) == [
        ("A", timedelta(minutes=5), MINUTE),
        ("A", DAY, MINUTE),
        ("B", HOUR, HOUR),
        ("B", timedelta(hours=2), HOUR),
    ]


def test_a_session_schedule_gives_its_calendar_security_a_minute_feed():
    from sugarloaf.market.contracts import Schedule

    at = NOW.time().replace(tzinfo=None)
    api, _ = feeds(
        BarsRequirement(Selection((A,))),
        BarsRequirement(Selection((B,))),
        triggers=(Trigger("scheduled", schedule=Schedule(at=at, calendar=A)),),
    )
    # The schedule is checked when data arrives, so its calendar ticks each minute.
    # B is read daily by the same node and stays daily.
    assert sorted(api.subscriptions) == [("A", MINUTE), ("B", DAY)]


def test_universe_members_are_subscribed_with_their_readers_feed_settings():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    definition = UniverseRequirement(LiquidityUniverse("u", 2))
    daily = BarsRequirement(Selection(universe="u"), lookback=5)
    services.install("n", ResourceUpdate((definition, daily), ()))
    assert api.universes["u"] == (daily, DAY)
    original = services.universes["u"]
    # A reader of the members' minute bars arrives: the source is recreated so LEAN
    # adds members at minute resolution from then on.
    minute = BarsRequirement(Selection(universe="u"), timedelta(minutes=1))
    services.install("m", ResourceUpdate((minute,), ()))
    assert api.universes["u"][1] == MINUTE
    assert api.disposed == [original]


def test_a_universe_nobody_reads_prices_from_adds_daily_members():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.install("n", ResourceUpdate((UniverseRequirement(LiquidityUniverse("u", 2)),), ()))
    assert api.universes["u"] == (None, DAY)


def test_a_named_security_takes_the_finest_feed_it_could_get_as_a_universe_member():
    # LEAN reads consolidators from a security's first subscription. A security a
    # node names is subscribed before any universe selects it, so it must start at
    # the universe's (finer) period or it would be stuck on the coarser one.
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    services.register(A, "A")
    services.install(
        "n",
        ResourceUpdate(
            (
                UniverseRequirement(LiquidityUniverse("u", 2)),
                BarsRequirement(Selection(universe="u"), timedelta(minutes=1)),
                BarsRequirement(Selection((A,))),
            ),
            (),
        ),
    )
    services.apply(EventBatch(0, NOW))
    assert ("A", MINUTE) in api.subscriptions
    assert ("A", DAY, MINUTE) in api.consolidators


def test_a_security_already_fed_at_a_coarser_period_is_refused_not_misread():
    api, services = feeds(BarsRequirement(Selection((A,))))
    assert api.subscriptions == [("A", DAY)]
    # A later node wants minute bars. LEAN would keep feeding consolidators from the
    # daily subscription, so the adapter stops instead of serving daily bars as minutes.
    services.install("m", ResourceUpdate((BarsRequirement(Selection((A,)), MINUTE),), ()))
    with pytest.raises(CapabilityError, match="request the finer interval from the start"):
        services.apply(EventBatch(1, NOW))


def test_a_feed_is_not_made_coarser_while_the_security_is_still_read():
    api, services = feeds(
        BarsRequirement(Selection((A,))), BarsRequirement(Selection((A,)), MINUTE)
    )
    services.install("n", ResourceUpdate((BarsRequirement(Selection((A,))),), ()))
    services.apply(EventBatch(1, NOW))
    # No second subscription, and the daily window is still built from minutes.
    assert api.subscriptions == [("A", MINUTE)]
    assert ("A", DAY, DAY) not in api.consolidators


def lean_api(**members):
    api = object.__new__(LeanAPI)
    api.lean = NS(**members)
    return api


def test_lean_consolidator_is_the_identity_one_when_the_feed_has_the_requested_interval():
    made = []

    class Identity:
        def __class_getitem__(cls, bar_type):
            return lambda: made.append(bar_type) or ("identity", bar_type)

    api = lean_api(
        IdentityDataConsolidator=Identity,
        TradeBar="TradeBar",
        QuoteBar="QuoteBar",
        TradeBarConsolidator=lambda period: ("built", period),
        QuoteBarConsolidator=lambda period: ("built-quotes", period),
    )
    daily = BarsRequirement(Selection((A,)))
    assert api.consolidator(daily, None, DAY) == ("identity", "TradeBar")
    two_hours = BarsRequirement(Selection((A,)), timedelta(hours=2))
    assert api.consolidator(two_hours, None, HOUR) == ("built", timedelta(hours=2))
    quotes = QuotesRequirement(Selection((A,)), MINUTE)
    assert api.consolidator(quotes, None, MINUTE) == ("identity", "QuoteBar")


@pytest.mark.parametrize(
    ("order_type", "method"),
    [
        ("market", "market_order"),
        ("market_on_open", "market_on_open_order"),
        ("market_on_close", "market_on_close_order"),
    ],
)
def test_lean_submit_uses_the_native_call_for_each_order_type(order_type, method):
    from sugarloaf.execution import OrderIntent

    calls = []

    def native(name):
        return lambda symbol, quantity, **options: calls.append((name, symbol, quantity, options))

    platform = NS(
        market_order=native("market_order"),
        market_on_open_order=native("market_on_open_order"),
        market_on_close_order=native("market_on_close_order"),
    )
    api = lean_api(
        OrderProperties=lambda: NS(time_in_force=None),
        TimeInForce=NS(DAY="native day", GOOD_TIL_CANCELED="native gtc"),
    )
    intent = OrderIntent("i", "t", A, -3, NOW, NOW + timedelta(days=1), order_type)
    api.submit(platform, "SYMBOL", intent)
    name, symbol, quantity, options = calls[0]
    assert (name, symbol, quantity) == (method, "SYMBOL", -3)
    assert options["asynchronous"] is True and options["tag"] == "i"
    assert options["order_properties"].time_in_force == "native gtc"
    for_the_day = OrderIntent("k", "t", A, 1, NOW, NOW + DAY, order_type, "day")
    api.submit(platform, "SYMBOL", for_the_day)
    assert calls[1][3]["order_properties"].time_in_force == "native day"
    with pytest.raises(CapabilityError, match="market, open and close"):
        api.submit(platform, "SYMBOL", OrderIntent("j", "t", A, 1, NOW, NOW + DAY, "limit"))


def test_lean_refusal_reads_an_invalid_ticket_and_passes_an_accepted_one():
    api = lean_api()
    response = NS(error_code="MarketOnCloseOrderTooLate", error_message="too late")
    refused = NS(status="OrderStatus.Invalid", get_most_recent_order_response=lambda: response)
    assert api.refusal(refused) == "MarketOnCloseOrderTooLate: too late"
    assert api.refusal(NS(status="OrderStatus.Submitted")) is None


def test_qc_gateway_accepts_three_order_types_and_reports_a_platform_refusal():
    from sugarloaf.core.errors import OrderRefused
    from sugarloaf.execution import ExecutionPolicy, OrderIntent

    class RefusingAPI(API):
        def submit(self, algorithm, symbol, intent):
            # LEAN returns a ticket with no order behind it and sends no order event.
            return NS(order_id=-10, refused="MarketOnCloseOrderTooLate: too late")

    api, services = feeds(BarsRequirement(Selection((A,))))
    gateway = QuantConnectGateway(services)
    for order_type in ("market", "market_on_open", "market_on_close"):
        for time_in_force in ("day", "gtc"):
            policy = ExecutionPolicy(order_type=order_type, time_in_force=time_in_force)
            assert gateway.capability(policy).supported
    services.api = RefusingAPI()
    intent = OrderIntent("i", "t", A, 1, NOW, NOW + DAY, "market_on_close")
    with pytest.raises(OrderRefused, match="QC_ORDER_REFUSED: MarketOnCloseOrderTooLate"):
        gateway.submit(intent)
    assert gateway.orders == {}


def test_qc_selection_passes_the_fundamentals_flag_to_a_universe_that_requires_it():
    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    definition = LiquidityUniverse("stocks", 1, require_fundamentals=True)
    services.install("n", ResourceUpdate((UniverseRequirement(definition),), ()))
    fund, stock = Symbol("f", "FUND"), Symbol("s", "STOCK")
    rows = [
        NS(symbol=fund, price=100, volume=10, dollar_volume=9000, has_fundamental_data=False),
        NS(symbol=stock, price=100, volume=10, dollar_volume=1000, has_fundamental_data=True),
    ]
    # The fund is the more liquid of the two and is left out before the count.
    assert api.selectors["stocks"](rows) == [stock]


def test_lean_security_initializer_gives_a_new_security_its_last_known_price():
    installed, applied = [], []
    platform = NS(
        add_security_initializer=installed.append,
        get_last_known_prices=lambda symbol: [("last", symbol)],
    )
    api = lean_api(ConstantSlippageModel=lambda rate: ("slippage", rate))
    api.setup(platform, 0.001, 2.0)
    security = NS(
        symbol="SYMBOL",
        set_slippage_model=lambda model: applied.append(model),
        set_leverage=lambda leverage: applied.append(("leverage", leverage)),
        set_market_price=lambda data: applied.append(data),
    )
    installed[0](security)
    assert applied == [("slippage", 0.001), ("leverage", 2.0), ("last", "SYMBOL")]


def test_quantbook_replays_a_daily_reader_on_daily_bars_and_ticks_at_each_close():
    from sugarloaf.adapters.quantconnect import QuantBookServices

    class DailyAPI(IndicatorAPI):
        def __init__(self):
            super().__init__()
            self.replayed = []

        def history_range(self, algorithm, symbol, source, start, end):
            self.replayed.append(source.interval)
            for index in (1, 2):
                yield native_bar(NOW + timedelta(days=index), 100 + index)

    source = BarsRequirement(Selection((A,)), lookback=1)

    class Reader(Signal):
        def __init__(self):
            super().__init__(source)
            self.times = []

        def evaluate(self, inputs, ctx):
            self.times.append(ctx.now)
            return super().evaluate(inputs, ctx)

    api, reader = DailyAPI(), Reader()
    provider = QuantBookServices(algorithm(), tickers={A: "A"}, api=api)
    runtime = GraphRuntime(compiled(reader), provider)
    end = NOW + timedelta(days=2)
    provider.prepare(NOW, end)
    for batch in provider.history_batches(NOW, end):
        runtime.process(batch)
    # The replay asks for the daily feed a deployment would subscribe, the identity
    # consolidator passes each bar through, and the node sees two closes.
    assert api.replayed == [DAY]
    assert ("A", DAY, DAY) in api.consolidators
    assert reader.times == [NOW + timedelta(days=1), NOW + timedelta(days=2)]
    assert reader.reads == [(101.0,), (102.0,)]


def test_factories_receive_stored_files_only_when_they_ask():
    from sugarloaf.adapters.quantconnect.launcher import load

    store = {"lab/list.json": "[1, 2]"}
    algorithm = NS(get_parameter=lambda name: {"start_date": "2024-01-02"}.get(name),
                   object_store=NS(contains_key=store.__contains__, read=store.__getitem__))

    def plain(parameter):
        return parameter("start_date")

    def with_files(parameter, stored):
        return parameter("start_date"), stored("lab/list.json"), stored("missing")

    assert load(plain, algorithm) == "2024-01-02"
    assert load(with_files, algorithm) == ("2024-01-02", "[1, 2]", None)


def test_qc_fundamental_universe_records_and_borrow_terms():
    from sugarloaf.market.contracts import (
        FundamentalsRequirement,
        FundamentalUniverse,
        ShortAvailabilityRequirement,
    )

    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)
    def apple_only(at, rows):
        return [r.instrument for r in rows if r.cik == "320193"]

    definition = FundamentalUniverse("filers", apple_only)
    fundamentals = FundamentalsRequirement(Selection(universe="filers"))
    short = ShortAvailabilityRequirement(Selection(universe="filers"))
    declared = (UniverseRequirement(definition), fundamentals, short)
    services.install("n", ResourceUpdate(declared, ()))
    apple, other = Symbol("aapl", "AAPL"), Symbol("x", "X")

    def native(cik, cap, price):
        return NS(company_reference=NS(cik=cik), market_cap=cap, price=price, dollar_volume=1e9,
                  has_fundamental_data=True)

    rows = [NS(symbol=apple, **vars(native("0000320193", 2.7e12, 179.66))),
            NS(symbol=other, **vars(native(None, 1e8, 5.0)))]
    assert api.selectors["filers"](rows) == [apple]
    member = services.memberships["filers"].members[0]
    platform.securities[apple] = NS(symbol=apple, fundamentals=native("320193", 2.8e12, 181.0),
                                    shortable_provider=NS(shortable_quantity=lambda *_: 1000,
                                                          fee_rate=lambda *_: 0.0025))
    record = services._fundamentals(member, NOW)
    assert (record.cik, record.market_cap, record.price) == ("320193", 2.8e12, 181.0)
    terms = services._short_availability(member, NOW)
    assert (terms.fee_rate, terms.shortable) == (0.0025, 1000.0)


def test_factories_receive_fetch_when_they_ask():
    from sugarloaf.adapters.quantconnect.launcher import load

    calls = []
    algorithm_ = NS(get_parameter=lambda name: None, object_store=NS(contains_key=lambda k: False),
                    download=lambda url, headers: calls.append((url, headers)) or "body")

    def factory(parameter, fetch):
        return fetch("https://example.test/a", {"User-Agent": "x"})

    assert load(factory, algorithm_) == "body"
    assert calls == [("https://example.test/a", {"User-Agent": "x"})]


def test_qc_fundamental_rule_failures_and_missing_borrow_data_are_not_silent():
    from sugarloaf.adapters.base import select
    from sugarloaf.market.contracts import FundamentalRecord, FundamentalUniverse

    api, platform = API(), algorithm()
    services = QuantConnectServices(platform, api=api)

    def unavailable(at, rows):
        raise NotReady("filing source does not cover this date")

    universe = UniverseRequirement(FundamentalUniverse("f", unavailable))
    services.install("n", ResourceUpdate((universe,), ()))
    apple = Symbol("aapl", "AAPL")
    row = NS(symbol=apple, company_reference=NS(cik="320193"), market_cap=1.0, price=1.0,
             dollar_volume=1.0)
    # The universe is unready rather than empty, and LEAN keeps the members it had.
    assert api.selectors["f"]([row]) == "UNCHANGED"
    assert services.memberships["f"].ready is False
    # A rule returning tickers instead of identities is an error, not an empty universe.
    tickers = FundamentalUniverse("t", lambda at, rows: ["AAPL"])
    with pytest.raises(ContractError):
        select(tickers, [FundamentalRecord(A, NOW, "320193")], NOW)
    # Definitions compare by name and version, not by the rule object; CIKs are unpadded.
    assert FundamentalUniverse("f", lambda a, r: []) == FundamentalUniverse("f", lambda a, r: [])
    assert FundamentalRecord(A, NOW, "0000320193").cik == "320193"
    # LEAN's default shortable provider is no data, not free and unlimited borrow.

    class NullShortableProvider:
        def fee_rate(self, *_):
            return 0.0

        def shortable_quantity(self, *_):
            return None

    services.symbols[A] = apple
    platform.securities[apple] = NS(symbol=apple, shortable_provider=NullShortableProvider())
    assert services._short_availability(A, NOW) is None
    with pytest.raises(CapabilityError):
        services._fundamentals(InstrumentId("unsubscribed"), NOW)
