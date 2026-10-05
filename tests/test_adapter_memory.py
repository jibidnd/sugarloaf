from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sugarloaf.adapters.memory import MemoryServices, SelectionRow
from sugarloaf.adapters.memory.services import resource_key
from sugarloaf.core.errors import CapabilityError, NotReady, SnapshotError
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.services import AccountSnapshot, OpenOrder
from sugarloaf.core.types import InstrumentId
from sugarloaf.market.contracts import (
    Bar,
    BarsRequirement,
    ETFConstituentsUniverse,
    Instrument,
    InstrumentRequirement,
    LiquidityUniverse,
    Observation,
    PortfolioRequirement,
    Quote,
    QuotesRequirement,
    ResourceUpdate,
    Selection,
    SeriesRequirement,
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
    services.apply(EventBatch(1, NOW, (Event("scheduled", key="select"),)))
    assert services.memberships["liquid"].members == (A,)
    assert services.additional_events == [Event("universe", key="liquid")]
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


def test_readiness_calendar_series_and_lazy_portfolio():
    req = BarsRequirement(Selection((A,)), lookback=2)
    series = SeriesRequirement("released", 1)
    services = MemoryServices(
        [Instrument(A, "A")],
        calendars={A: {"regular": ((NOW - timedelta(hours=4), NOW + timedelta(minutes=1)),)}},
    )
    services.install(
        "n",
        ResourceUpdate(
            (req, series, PortfolioRequirement(), InstrumentRequirement(Selection((A,)))), ()
        ),
    )
    services.put_bars(req, [bar()])
    services.put_series(series, [Observation(NOW, NOW + timedelta(minutes=1), 9)])
    services.apply(EventBatch(0, NOW))
    ctx = services.context(node_id="n")
    assert services.read_counts == {}
    with pytest.raises(NotReady):
        services.ready("n", ctx)
    with pytest.raises(NotReady):
        ctx.market.series(series)
    assert ctx.instruments.is_open(A, NOW, "regular")
    assert not ctx.instruments.is_open(A, NOW + timedelta(minutes=1), "regular")
    assert not ctx.portfolio.borrow(A, 1).supported
