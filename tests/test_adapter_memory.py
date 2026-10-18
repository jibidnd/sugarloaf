from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryServices, SelectionRow
from sugarloaf.adapters.memory.services import resource_key
from sugarloaf.core.errors import CapabilityError, NotReady, SnapshotError
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.services import AccountSnapshot, OpenOrder
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    ETFConstituentsUniverse,
    Instrument,
    InstrumentRequirement,
    LiquidityUniverse,
    PortfolioRequirement,
    Quote,
    QuotesRequirement,
    ResourceUpdate,
    Selection,
    Trigger,
    UniverseRequirement,
)

NOW = datetime(2025, 11, 28, 17, 59, tzinfo=UTC)  # US early-close session.
A, B = InstrumentId("A"), InstrumentId("B")


def bar(at=NOW):
    return Bar(A, at - timedelta(minutes=1), at, at, 100, 101, 99, 100, 1000)


def test_cutoff_dedup_shared_ownership_quotes_and_snapshot_guards():
    req = BarsRequirement(Selection((A,)), timedelta(minutes=1), lookback=2)
    quote = QuotesRequirement(Selection((A,)))
    services = MemoryServices([Instrument(A, "A")])
    for node in ("a", "b"):
        services.install(node, ResourceUpdate((req, quote), ()))
    services.put_bars(
        req, (bar(NOW - timedelta(minutes=1)), bar(), bar(), bar(NOW + timedelta(minutes=1)))
    )
    services.put_quotes(quote, (Quote(A, NOW, NOW, 99, 101, 30, 20),))
    batch = EventBatch(1, NOW)
    services.apply(batch)
    ctx = services.context(batch, "a")
    assert [b.end for b in ctx.market.bars(req, A)] == [NOW - timedelta(minutes=1), NOW]
    assert ctx.market.quotes(quote, A)[0].ask == 101
    with pytest.raises(CapabilityError):
        ctx.market.bars(replace(req, lookback=1), A)
    with pytest.raises(CapabilityError):
        ctx.portfolio.snapshot()
    reader = ctx.market
    services.install("a", ResourceUpdate((), ()))
    assert services.owners[resource_key(req, A)] == {"b"}
    with pytest.raises(SnapshotError):
        reader.bars(req, A)


def test_universe_refresh_empty_missing_weight_and_retention():
    definition = LiquidityUniverse(
        "liquid", 1, refresh=Trigger("scheduled", key="select"), minimum_residence=timedelta(days=1)
    )
    req = BarsRequirement(Selection(universe="liquid"))
    services = MemoryServices()
    services.install("node", ResourceUpdate((UniverseRequirement(definition), req), ()))
    services.set_selection(
        "liquid", [SelectionRow(B, 20, 5, 100), SelectionRow(A, 20, 5, 100)], NOW
    )
    services.apply(EventBatch(0, NOW, (Event("data"),)))
    assert "liquid" not in services.memberships
    # Admission returns the facts it discovered: here, that membership changed.
    discovered = services.apply(EventBatch(1, NOW, (Event("scheduled", key="select"),)))
    assert services.memberships["liquid"].members == (A,)
    assert discovered == [Event("universe", key="liquid")]
    services.set_selection("liquid", None, NOW)
    services.apply(EventBatch(2, NOW, (Event("scheduled", key="select"),)))
    assert services.memberships["liquid"].members == (A,)
    services.set_selection("liquid", [], NOW)
    services.apply(EventBatch(3, NOW, (Event("scheduled", key="select"),)))
    assert services.memberships["liquid"].members == ()
    assert resource_key(req, A) in services.retained_resources
    services.set_account(
        AccountSnapshot({A: 1}, 100, {"USD": 0}, {A: 100}, (OpenOrder("x", A, -1),))
    )
    services.apply(EventBatch(4, NOW + timedelta(days=2)))
    assert resource_key(req, A) in services.retained_resources
    etf = ETFConstituentsUniverse("etf", A, missing_weight="error")
    services.install("etf", ResourceUpdate((UniverseRequirement(etf),), ()))
    services.set_selection("etf", [SelectionRow(B)], NOW)
    services.apply(EventBatch(5, NOW + timedelta(days=2), (Event("universe"),)))
    with pytest.raises(NotReady):
        services.context(node_id="etf").market.membership("etf")


def test_readiness_calendar_and_lazy_portfolio():
    req = BarsRequirement(Selection((A,)), lookback=2)
    services = MemoryServices(
        [Instrument(A, "A")],
        calendars={A: {"regular": ((NOW - timedelta(hours=4), NOW + timedelta(minutes=1)),)}},
    )
    services.install(
        "n",
        ResourceUpdate(
            (req, PortfolioRequirement(), InstrumentRequirement(Selection((A,)))), ()
        ),
    )
    services.put_bars(req, [bar()])
    services.apply(EventBatch(0, NOW))
    ctx = services.context(node_id="n")
    assert services.read_counts == {}
    with pytest.raises(NotReady):
        services.ready("n", ctx)
    assert ctx.instruments.is_open(A, NOW, "regular")
    assert not ctx.instruments.is_open(A, NOW + timedelta(minutes=1), "regular")
    assert not ctx.portfolio.borrow(A, 1).supported


def test_partial_readiness_lets_a_node_use_the_ready_members():
    # A has two bars of history; B, like a newly listed stock, has only one.
    strict = BarsRequirement(Selection((A, B)), timedelta(minutes=1), lookback=2)
    partial = replace(strict, require_all=False)
    services = MemoryServices([Instrument(A, "A"), Instrument(B, "B")])
    services.install("strict", ResourceUpdate((strict,), ()))
    services.install("partial", ResourceUpdate((partial,), ()))
    short_history = replace(bar(), instrument=B)
    # Both requirements share one backing resource, so one put serves both readers.
    services.put_bars(strict, (bar(NOW - timedelta(minutes=1)), bar(), short_history))
    services.apply(EventBatch(0, NOW))
    # The default still withdraws the whole node when any member is unready.
    with pytest.raises(NotReady, match="Insufficient history: B"):
        services.ready("strict", services.context(node_id="strict"))
    # Opting out lets the node evaluate and see exactly which members it can read.
    ctx = services.context(node_id="partial")
    services.ready("partial", ctx)
    assert ctx.market.ready(partial) == (A,)
    with pytest.raises(NotReady):
        ctx.market.bars(partial, B)


def test_session_close_counts_trading_sessions_across_a_weekend():
    friday_close = datetime(2025, 1, 10, 21, tzinfo=UTC)
    monday_close = datetime(2025, 1, 13, 21, tzinfo=UTC)
    tuesday_close = datetime(2025, 1, 14, 21, tzinfo=UTC)
    sessions = tuple(
        (close - timedelta(hours=6, minutes=30), close)
        for close in (friday_close, monday_close, tuesday_close)
    )
    services = MemoryServices([Instrument(A, "A")], calendars={A: {"regular": sessions}})
    services.install("n", ResourceUpdate((InstrumentRequirement(Selection((A,))),), ()))
    services.apply(EventBatch(0, friday_close))
    catalog = services.context(node_id="n").instruments
    # During Friday's session the next close is Friday's own.
    assert catalog.session_close(A, friday_close - timedelta(hours=1)) == friday_close
    # At the close itself the search is exclusive: one session later is Monday,
    # not Saturday, which is what a one-calendar-day horizon would have produced.
    assert catalog.session_close(A, friday_close) == monday_close
    assert catalog.session_close(A, friday_close, 2) == tuesday_close
    with pytest.raises(NotReady, match="no such later session"):
        catalog.session_close(A, friday_close, 3)


def test_liquidity_universe_can_require_fundamentals_before_taking_the_count():
    from sugarloaf.adapters.base import select
    from sugarloaf.core.errors import NotReady

    rows = [
        SelectionRow(A, 20, 5, 900, has_fundamentals=False),  # a fund: most liquid
        SelectionRow(B, 20, 5, 100, has_fundamentals=True),
    ]
    assert select(LiquidityUniverse("all", 1), rows) == (A,)
    stocks = LiquidityUniverse("stocks", 1, require_fundamentals=True)
    assert select(stocks, rows) == (B,)
    # A provider that cannot say is not taken to mean "no".
    with pytest.raises(NotReady, match="fundamentals"):
        select(stocks, [SelectionRow(A, 20, 5, 900)])


def test_a_security_listed_twice_is_selected_once_and_does_not_use_up_a_place():
    from sugarloaf.adapters.base import select

    rows = [SelectionRow(A, 20, 5, 900), SelectionRow(A, 20, 5, 800), SelectionRow(B, 20, 5, 100)]
    assert select(LiquidityUniverse("two", 2), rows) == (A, B)
    etf = ETFConstituentsUniverse("etf", A, count=2)
    weighted = [SelectionRow(A, weight=w) for w in (0.5, 0.4)] + [SelectionRow(B, weight=0.1)]
    assert select(etf, weighted) == (A, B)


def test_fundamental_universe_and_reads_are_point_in_time():
    from sugarloaf.market.contracts import (
        FundamentalRecord,
        FundamentalsRequirement,
        FundamentalUniverse,
        ShortAvailability,
        ShortAvailabilityRequirement,
    )

    C = InstrumentId("C")
    seen = []

    def rule(at, rows):
        seen.append(at)
        # Issuers on a strategy's own list; an instrument absent from the rows is ignored.
        return [r.instrument for r in rows if r.cik in {"111", "222"}] + [InstrumentId("Z")]

    definition = FundamentalUniverse("filers", rule)
    fundamentals = FundamentalsRequirement(Selection(universe="filers"), max_age=timedelta(days=2))
    short = ShortAvailabilityRequirement(Selection(universe="filers"))
    earlier, later = NOW - timedelta(days=1), NOW + timedelta(days=1)
    services = MemoryServices(
        fundamentals={A: [FundamentalRecord(A, earlier, "111", 3e7, 2.5),
                          FundamentalRecord(A, later, "111", 9e7, 7.5)]},
        short={A: [ShortAvailability(A, earlier, fee_rate=0.4, shortable=5000)]},
    )
    declared = (UniverseRequirement(definition), fundamentals, short)
    services.install("n", ResourceUpdate(declared, ()))
    rows = [FundamentalRecord(i, NOW, cik) for i, cik in ((A, "111"), (B, "999"), (C, "222"))]
    services.set_selection("filers", rows, NOW)
    services.apply(EventBatch(0, NOW, (Event("universe"),)))
    assert services.memberships["filers"].members == (A, C) and seen == [NOW]
    ctx = services.context(node_id="n")
    record = ctx.market.fundamentals(fundamentals, A)
    # The later record is not yet known; the earlier one is within max_age.
    assert (record.market_cap, record.price) == (3e7, 2.5)
    assert ctx.market.short_availability(short, A).fee_rate == 0.4
    assert ctx.market.ready(fundamentals) == (A,)
    with pytest.raises(NotReady):
        ctx.market.fundamentals(fundamentals, C)
    with pytest.raises(CapabilityError):
        ctx.market.fundamentals(fundamentals, B)
    # Four days on, the newest record (dated a day after NOW) is three days old: stale.
    services.apply(EventBatch(1, NOW + timedelta(days=4)))
    with pytest.raises(NotReady, match="Stale"):
        services.context(node_id="n").market.fundamentals(fundamentals, A)
