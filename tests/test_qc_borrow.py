"""Borrow terms read from a security's shortable provider (QuantConnect adapter, pure part)."""

from datetime import datetime, timezone

from sugarloaf.adapters.quantconnect.adapter import borrow_terms
from sugarloaf.core.primitives import InstrumentId

STOCK = InstrumentId("USA:STOCK")
AT = datetime(2026, 3, 3, 5, tzinfo=timezone.utc)


class Provider:
    """Answers by the date it is asked about, as LEAN's Interactive Brokers provider does."""

    def __init__(self, table):
        self.table, self.asked = table, []

    def fee_rate(self, symbol, local):
        self.asked.append(local)
        return self.table.get(local.date(), (0.0, None))[0]

    def shortable_quantity(self, symbol, local):
        return self.table.get(local.date(), (0.0, None))[1]


class NullShortableProvider:
    pass


class Security:
    def __init__(self, provider):
        self.shortable_provider, self.symbol = provider, "STOCK"


def test_terms_are_read_at_the_local_time_and_stamped_with_the_clock():
    local = datetime(2026, 3, 3)
    provider = Provider({local.date(): (0.35, 2e5)})
    record = borrow_terms(STOCK, Security(provider), local, AT)
    assert (record.instrument, record.time, record.fee_rate, record.shortable) == (
        STOCK, AT, 0.35, 2e5)
    assert provider.asked == [local]


def test_a_day_without_a_record_has_no_quantity():
    record = borrow_terms(STOCK, Security(Provider({})), datetime(2026, 3, 4), AT)
    assert record.shortable is None and record.fee_rate == 0.0


def test_no_terms_without_a_provider():
    assert borrow_terms(STOCK, Security(None), datetime(2026, 3, 3), AT) is None
    assert borrow_terms(STOCK, Security(NullShortableProvider()), datetime(2026, 3, 3), AT) is None
