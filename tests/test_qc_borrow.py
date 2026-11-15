"""Borrow terms on the QuantConnect adapter, without LEAN.

``borrow_terms`` reads one security's terms from its shortable provider. The adapter
delivers every borrow-selected member's terms once per trading session, at the session's
first event (``QuantConnectAdapter._deliver_borrow``, called from ``on_data``, the
scheduled function built in ``schedule`` and ``on_securities_changed``), read with that
session's date. The provider below answers by calendar date only, as LEAN's
``LocalDiskShortableProvider`` does: no row for a date means a null quantity and a zero fee.
"""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sugarloaf.adapters.quantconnect.adapter import QuantConnectAdapter, borrow_terms
from sugarloaf.core.inputs import Custom, Schedule, Selection
from sugarloaf.core.primitives import InstrumentId

STOCK = InstrumentId("USA:STOCK")
AT = datetime(2026, 3, 3, 5, tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")


class Provider:
    """Answers by the calendar date it is asked about, as LEAN's disk provider does."""

    def __init__(self, table):
        self.table, self.asked = table, []

    def fee_rate(self, symbol, local):
        row = self.table.get(local.date())
        return row[0] if row else 0.0

    def shortable_quantity(self, symbol, local):
        self.asked.append(local)
        row = self.table.get(local.date())
        return row[1] if row else None


class NullShortableProvider:
    pass


class Security:
    def __init__(self, provider, symbol="STOCK"):
        self.shortable_provider, self.symbol = provider, symbol


def test_terms_are_read_at_the_local_time_and_stamped_with_the_clock():
    local = datetime(2026, 3, 3)
    provider = Provider({local.date(): (0.35, 2e5)})
    record = borrow_terms(STOCK, Security(provider), local, AT)
    assert (record.instrument, record.time, record.fee_rate, record.shortable) == (
        STOCK, AT, 0.35, 2e5)
    assert provider.asked == [local]


def test_a_date_without_a_row_is_a_record_with_neither_quantity_nor_fee():
    record = borrow_terms(STOCK, Security(Provider({})), datetime(2026, 3, 4), AT)
    assert record is not None
    assert (record.shortable, record.fee_rate) == (None, None)


def test_no_terms_without_a_provider():
    assert borrow_terms(STOCK, Security(None), datetime(2026, 3, 3), AT) is None
    assert borrow_terms(STOCK, Security(NullShortableProvider()), datetime(2026, 3, 3), AT) is None


# ------------------------------------------------------------- session delivery


class Symbol:
    def __init__(self, ticker):
        self.id, self.value = f"{ticker} R735QTJ8XC9X", ticker

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Symbol) and other.id == self.id


class Hours:
    """Weekday sessions 09:30-16:00 New York, no holidays."""

    def get_next_market_open(self, local, extended):
        day = local.date()
        while True:
            opened = datetime.combine(day, time(9, 30))
            if day.weekday() < 5 and opened > local:
                return opened
            day += timedelta(days=1)


class ListedSecurity:
    def __init__(self, symbol, provider):
        self.symbol, self.shortable_provider = symbol, provider
        zone = SimpleNamespace(id="America/New_York")
        self.exchange = SimpleNamespace(hours=Hours(), time_zone=zone)


class Algorithm:
    """The QCAlgorithm members the borrow path touches, with a settable clock."""

    def __init__(self, securities):
        self.settings = SimpleNamespace()
        self.securities = securities
        self.is_warming_up = False
        self.scheduled = []
        self.schedule = SimpleNamespace(on=lambda d, t, fn: self.scheduled.append(fn))
        self.date_rules = SimpleNamespace(every_day=lambda symbol: ("every_day", symbol))
        self.time_rules = SimpleNamespace(after_market_open=lambda symbol, m: ("after", m))
        self.utc_time = None

    def add_security_initializer(self, initializer):
        pass

    def at(self, local: datetime):
        self.utc_time = local.replace(tzinfo=NEW_YORK).astimezone(timezone.utc)


class Recorder:
    """The runner's callbacks, recorded in the order the adapter makes them."""

    def __init__(self):
        self.events = []

    def on_data(self, arrival):
        self.events.append(("data", arrival.custom.get("borrow", ())))

    def on_scheduled(self, name, warming_up=False):
        self.events.append(("scheduled", name))

    def on_securities_changed(self, universe, added, removed, warming_up=False):
        self.events.append(("changed", added))


def _adapter(table):
    spy, stock = Symbol("SPY"), Symbol("STOCK")
    provider = Provider(table)
    securities = {spy: ListedSecurity(spy, None), stock: ListedSecurity(stock, provider)}
    algorithm = Algorithm(securities)
    spy_id = InstrumentId("USA:SPY")
    adapter = QuantConnectAdapter(algorithm, {spy_id: "SPY"}, lean=SimpleNamespace())
    adapter.symbols[spy_id] = spy
    adapter.runner = Recorder()
    # A universe "top" whose rule selected STOCK; no feeds, so a join fills nothing.
    adapter.universes["top"] = (None, (), None)
    adapter._selected["top"] = (adapter.bind(stock),)
    adapter.add_custom(Custom("borrow", Selection(universe="top")))
    adapter.schedule(Schedule("open", calendar=spy_id, after_open=1))
    return adapter, algorithm, stock, provider


def _join(adapter, stock):
    changes = SimpleNamespace(added_securities=[SimpleNamespace(symbol=stock)],
                              removed_securities=[])
    adapter.on_securities_changed(changes)


def test_a_saturday_join_gets_the_monday_terms_before_the_monday_scheduled_callback():
    monday = date(2026, 3, 9)
    adapter, algorithm, stock, provider = _adapter({monday: (0.012, 5e4)})
    algorithm.at(datetime(2026, 3, 7, 0, 0))  # Saturday 00:00: selection for Friday's filing
    _join(adapter, stock)
    assert adapter.runner.events == [("changed", (adapter.bind(stock),))]
    algorithm.at(datetime(2026, 3, 9, 9, 31))  # Monday's first event: the 09:31 schedule
    algorithm.scheduled[0]()
    (kind, records), scheduled = adapter.runner.events[1:]
    assert kind == "data" and scheduled == ("scheduled", "open")
    (record,) = records
    assert (record.instrument, record.fee_rate, record.shortable) == (
        adapter.bind(stock), 0.012, 5e4)
    assert provider.asked == [datetime(2026, 3, 9)]


def test_terms_carry_the_session_date_and_arrive_once_per_session():
    tuesday, wednesday = date(2026, 3, 10), date(2026, 3, 11)
    adapter, algorithm, stock, provider = _adapter(
        {tuesday: (0.01, 1e5), wednesday: (0.02, 2e5)})
    algorithm.at(datetime(2026, 3, 9, 0, 0))
    _join(adapter, stock)  # Monday 00:00 is the first event of Monday's session
    data = SimpleNamespace(bars={}, quote_bars={})
    for day in (10, 11):
        algorithm.at(datetime(2026, 3, day, 9, 31))
        algorithm.scheduled[0]()
        algorithm.at(datetime(2026, 3, day, 16, 0))  # the daily bar's slice
        adapter.on_data(data)
    delivered = [records for kind, records in adapter.runner.events if kind == "data" and records]
    # Monday (no row: a record with neither quantity nor fee), Tuesday, Wednesday.
    assert [(r.time.date(), r.fee_rate, r.shortable) for (r,) in delivered] == [
        (date(2026, 3, 9), None, None), (tuesday, 0.01, 1e5), (wednesday, 0.02, 2e5)]
    assert provider.asked == [datetime(2026, 3, 9), datetime(2026, 3, 10), datetime(2026, 3, 11)]
    # Each session's terms precede that session's scheduled callback.
    order = [kind for kind, _ in adapter.runner.events if kind != "changed"]
    assert order[:3] == ["data", "data", "scheduled"]
