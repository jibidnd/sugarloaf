"""Deterministic fixture provider; no local indicator calculation fallback."""

from sugarloaf.adapters.base import ProviderServices, SelectionRow, resource_key, select
from sugarloaf.core.errors import CapabilityError, NotReady
from sugarloaf.core.services import AccountSnapshot
from sugarloaf.market.contracts import Capability

__all__ = ["MemoryServices", "SelectionRow", "select"]


class MemoryServices(ProviderServices):
    """Explicit observations, account, and calendar fixtures for controlled replay.

    Args:
        instruments (Iterable[Instrument]): Neutral metadata available to declared readers.
        account (AccountSnapshot | None): Initial authoritative fixture snapshot; defaults to USD
            100,000 cash.
        constraints (PortfolioConstraints | None): Execution/Context exposure limits.
        calendars (Mapping | None): Instrument-to-session mappings of aware open/close pairs.
        borrow (Mapping[InstrumentId, float] | None): Confirmed shortable quantities by instrument.
        fundamentals (Mapping[InstrumentId, Sequence[FundamentalRecord]] | None): Records by
            instrument, any order; a read gives the latest one dated at or before graph time.
        short (Mapping[InstrumentId, Sequence[ShortAvailability]] | None): Borrow terms likewise.
        cost_rate (float): Proportional fixture cost per absolute trade notional.

    Writes invalidate existing Contexts; timestamps govern later visibility.
    Native indicators are unsupported, and this provider is not a market simulator.
    """

    def __init__(
        self,
        instruments=(),
        account=None,
        constraints=None,
        calendars=None,
        borrow=None,
        cost_rate=0.0,
        fundamentals=None,
        short=None,
    ):
        # Share declaration guards, readiness and causal read filtering with other
        # providers. Only the backing observations/account/calendar are fixtures.
        super().__init__(constraints, account_services=True)
        self.instruments = {i.id: i for i in instruments}
        self.account = account or AccountSnapshot({}, 100000, {"USD": 100000}, {})
        self.calendars = calendars or {}
        self.borrow_available = borrow or {}
        self.fundamental_records = {k: tuple(v) for k, v in (fundamentals or {}).items()}
        self.short_records = {k: tuple(v) for k, v in (short or {}).items()}
        self.cost_rate = cost_rate
        # Buffers store supplied neutral observations, not simulated market ticks.
        # Their observation and availability timestamps determine later visibility.
        self.buffers = {}

    def _retained_instruments(self):
        # Resource retention follows explicit account exposure even when dynamic
        # selection no longer includes the instrument. This does not create trades.
        return set(self.account.holdings) | {o.instrument for o in self.account.open_orders}

    def _session_times(self, instrument, day):
        # A fixture supplies actual session boundaries; no weekday/holiday rule
        # is invented when the requested trading day has no configured session.
        sessions = self.calendars.get(instrument, {}).get("regular", ())
        return next(((start, end) for start, end in sessions if start.date() == day), None)

    def _session_close(self, instrument, after, count):
        if instrument not in self.calendars:
            raise CapabilityError("An explicit exchange calendar fixture is required")
        # Closes strictly after the given instant, earliest first. The count-th one
        # is the end of a horizon of that many sessions. For example, with sessions
        # closing Friday and Monday, one session after Friday's close is Monday's.
        closes = sorted(
            end for _, end in self.calendars[instrument].get("regular", ()) if end > after
        )
        if len(closes) < count:
            # The fixture simply does not extend that far. That is missing
            # evidence, not a reason to guess a weekday rule.
            raise NotReady("Calendar fixture has no such later session")
        return closes[count - 1]

    def _session_open(self, instrument, after, count):
        if instrument not in self.calendars:
            raise CapabilityError("An explicit exchange calendar fixture is required")
        # Opens strictly after the given instant, earliest first, as for closes:
        # asked at Friday's close, the first one is Monday's open.
        opens = sorted(
            start for start, _ in self.calendars[instrument].get("regular", ()) if start > after
        )
        if len(opens) < count:
            raise NotReady("Calendar fixture has no such later session")
        return opens[count - 1]

    def set_account(self, account: AccountSnapshot) -> None:
        """Replace the account snapshot and invalidate previously created contexts."""
        # Invalidate snapshots immediately so reconciliation cannot reuse account
        # state from before a caller-applied fill or account replacement.
        self.account = account
        self._changed()

    def put_bars(self, requirement, bars):
        """Merge bars by resource and end time; latest values replace equal-time revisions."""
        self._put(requirement, bars, lambda b: b.end)

    def put_quotes(self, requirement, quotes):
        """Merge quotes by backing resource and observation time, then invalidate Contexts."""
        self._put(requirement, quotes, lambda q: q.time)

    def _put(self, requirement, values, timestamp):
        # Merge by exact observation timestamp, preserving chronological order.
        # A caller-supplied revision replaces that timestamp's retained contents;
        # this fixture is not a historical database of all revision vintages.
        for value in values:
            key = resource_key(requirement, value.instrument)
            merged = {timestamp(x): x for x in self.buffers.get(key, ())}
            merged[timestamp(value)] = value
            self.buffers[key] = tuple(merged[t] for t in sorted(merged))
        self._changed()

    def set_selection(self, name, rows, cutoff):
        """None means unchanged; an empty iterable is an explicit empty selection."""
        # Preserve the distinction between no update and an explicitly empty
        # universe. Shared apply() admits these rows only at an eligible refresh.
        if rows is not None:
            self.selection_inputs[name] = (tuple(rows), cutoff)
            self._changed()

    def _data(self, req, instrument, cutoff):
        # Supply raw fixture values here. ProviderServices filters cutoff/receipt,
        # lookback and freshness separately for each authorized node read.
        return self.buffers.get(resource_key(req, instrument), ())

    def _fundamentals(self, instrument, cutoff):
        return _latest(self.fundamental_records.get(instrument, ()), cutoff)

    def _short_availability(self, instrument, cutoff):
        return _latest(self.short_records.get(instrument, ()), cutoff)

    def _instrument(self, instrument):
        if instrument not in self.instruments:
            raise CapabilityError(f"Unknown instrument: {instrument}")
        return self.instruments[instrument]

    def _account(self):
        return self.account

    def _open(self, instrument, at, session):
        if instrument not in self.calendars:
            raise CapabilityError("An explicit exchange calendar fixture is required")
        # Session membership includes the open and excludes the close. Extended
        # sessions must be supplied explicitly just like regular sessions.
        return any(start <= at < end for start, end in self.calendars[instrument].get(session, ()))

    def _borrow(self, instrument, quantity):
        # Missing borrow is not unlimited borrow: the fixture must affirm quantity.
        available = self.borrow_available.get(instrument)
        return Capability(
            available is not None and available >= quantity,
            "Borrow availability not supplied or insufficient",
        )

    def _buying_power(self, instrument, quantity):
        account = self._account()
        price = account.prices.get(instrument)
        held = account.holdings.get(instrument, 0)
        # Permit purely reducing orders; otherwise use a simple NAV/notional bound.
        # This is a controlled fixture capability, not a brokerage margin model.
        if held * quantity < 0 and abs(quantity) <= abs(held):
            return Capability(True)
        return Capability(
            price is not None and abs(quantity * price) <= account.nav,
            "Fixture gross buying power exceeded or no price",
        )

    def _cost_estimate(self, instrument, quantity, price):
        # Return an estimate only. MemoryGateway does not automatically charge
        # fees/slippage or synthesize a fill from this proportional cost setting.
        return abs(quantity * price) * self.cost_rate


def _latest(records, cutoff):
    known = [r for r in records if r.time <= cutoff]
    return max(known, key=lambda r: r.time) if known else None
