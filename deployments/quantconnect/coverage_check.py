"""Native check of the adapter paths the other native checks leave out, declared together.

One node declares, over two weeks of 2025 with a ten-day ``WarmUp``:

* ``Custom("borrow")`` on the fundamental-universe members, with Interactive Brokers borrow
  data: ``shortable_quantity`` and ``fee_rate`` of LEAN's shortable provider, read once per
  trading session at its first event (``QuantConnectAdapter._deliver_borrow``). The report
  asserts no member's terms arrive twice in a session, every record is stamped in the
  session it arrives in, and no scheduled call runs before its session's terms.
* ``Custom("earnings")`` on SPY and AAPL: ``add_data(EODHDUpcomingEarnings)`` and
  ``slice.get`` in ``QuantConnectAdapter._custom``. AAPL has no other declaration, so its
  listings reach the node only through the earnings path; SPY, an ETF, has none.
* ``ETFConstituentsUniverse`` on SPY (the two heaviest constituents, daily refresh):
  ``universe.etf`` in ``add_universe``; members join in ``on_securities_changed``.
* ``FundamentalUniverse`` with a monthly cadence (the three energy stocks with the largest
  dollar volume): ``FundamentalUniverse.usa`` and ``universe_settings.schedule.on``.
* ``Schedule`` at 10:02 in Chicago with no calendar: ``time_rules.at`` with a NodaTime zone and
  ``date_rules.every_day`` with SPY's symbol (``_default_calendar``), in ``schedule``.
* ``Quotes`` on SPY at minute: ``slice.quote_bars`` in ``on_data``.
* ``Indicator`` kinds ``sma``, ``rsi`` and ``donchian`` on SPY at the minute feed (an identity
  consolidator) and at five minutes (a ``TradeBarConsolidator``), in ``_create_indicator``;
  a daily ``sma`` on each fundamental-universe member, whose consolidator
  ``on_securities_changed`` removes when the member leaves.
* Minute and five-minute bars of SPY and daily bars of each universe's members.

For every declaration the node counts arrivals (records delivered to it) and the distinct
end times (bars) or record times (listings, membership changes, scheduled calls) among
them, and for every indicator the distinct value end times it observed and whether it
became ready. ``_report`` writes the counts to the report record ``coverage_check`` (a
``RUN_RECORD`` log line) and to one ``COVERAGE_CHECK`` log line, then raises
``AssertionError`` naming each declaration with no arrival, or when a callback's
``ctx.warming_up`` disagrees with the clock, when the schedule calls on a weekend, or when
borrow terms arrive out of their session, so a run that misses a path ends with a runtime error. The
graph's output port emits views, so this is a forecast run: nothing is traded.

    sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/coverage_check.toml
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import (
    Bars,
    Custom,
    ETFConstituentsUniverse,
    FundamentalUniverse,
    Indicator,
    Quotes,
    Schedule,
    Selection,
    WarmUp,
)
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.views import ScoreView
from sugarloaf.deployment import Deployment, Environment

SPY, AAPL = InstrumentId("USA:SPY"), InstrumentId("USA:AAPL")
NEW_YORK = ZoneInfo("America/New_York")
FIVE_MINUTES = timedelta(minutes=5)
ENERGY = 309  # Morningstar sector code
WARM_UP_DAYS = 10
LOGGED_FILLS = 40


def energy_leaders(at, records):
    """The three energy stocks priced at 5 or more with the largest dollar volume."""
    rows = [
        r for r in records
        if r.sector == ENERGY and r.price is not None and r.price >= 5 and r.dollar_volume
    ]
    rows.sort(key=lambda r: (-r.dollar_volume, r.instrument.value))
    return [r.instrument for r in rows[:3]]


# Bars and quotes; names keep the two SPY bar feeds apart in data.series.
SPY_MINUTE = Bars(Selection((SPY,)), "minute", window=30, name="spy_1m")
SPY_FIVE = Bars(Selection((SPY,)), "minute", interval=FIVE_MINUTES, window=12, name="spy_5m")
SPY_QUOTES = Quotes(Selection((SPY,)), "minute", window=5)
EARNINGS = Custom("earnings", Selection((SPY, AAPL)))
# Interactive Brokers borrow terms of the energy members, once per trading session.
BORROW = Custom("borrow", Selection(universe="energy_top3"))
ETF_TOP = ETFConstituentsUniverse("spy_top2", etf=SPY, count=2)
ENERGY_TOP = FundamentalUniverse("energy_top3", cadence="monthly", rule=energy_leaders)
ETF_DAILY = Bars(Selection(universe="spy_top2"), "daily", window=5, name="etf_daily")
ENERGY_DAILY = Bars(Selection(universe="energy_top3"), "daily", window=5, name="energy_daily")
SCHEDULE = Schedule("chicago_1002", at=time(10, 2), timezone="America/Chicago")
INDICATORS = tuple(
    Indicator(f"{kind}_{label}", kind, period, Selection((SPY,)), "minute", interval, window=5)
    for label, interval in (("1m", None), ("5m", FIVE_MINUTES))
    for kind, period in (("sma", 10), ("rsi", 14), ("donchian", 20))
) + (
    # One per fundamental-universe member: created when a member joins, its consolidator
    # removed when the member leaves (on_securities_changed).
    Indicator("sma_energy", "sma", 3, Selection(universe="energy_top3"), "daily", window=3),
)


class Tally:
    """Arrivals of one declaration: how many, the distinct times among them, and when."""

    def __init__(self):
        self.arrivals = 0
        self.times: set[datetime] = set()
        self.during_warm_up = 0
        self.first: datetime | None = None

    def add(self, at: datetime, warming_up: bool, count: int = 1) -> None:
        self.arrivals += count
        self.times.add(at)
        self.during_warm_up += count if warming_up else 0
        self.first = at if self.first is None else min(self.first, at)

    def summary(self) -> dict:
        return {
            "arrivals": self.arrivals,
            "distinct_times": len(self.times),
            "during_warm_up": self.during_warm_up,
            "first": self.first.isoformat() if self.first else None,
        }


class CoverageProbe:
    """Count every declaration's arrivals and every indicator's values; emit nothing."""

    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}
    inputs = (SPY_MINUTE, SPY_FIVE, SPY_QUOTES, EARNINGS, BORROW, ETF_TOP, ENERGY_TOP,
              ETF_DAILY, ENERGY_DAILY)
    indicators = INDICATORS
    schedules = (SCHEDULE,)
    warm_up = WarmUp(span=timedelta(days=WARM_UP_DAYS))

    def __init__(self, start: date):
        # The live period starts at midnight in New York on the start date; earlier
        # callbacks belong to the warm-up replay. Every callback's ctx.warming_up is
        # compared with this clock rule; warm_up_mismatches counts the disagreements.
        self.live_from = datetime.combine(start, time(0), NEW_YORK).astimezone(UTC)
        self.config = {"start": start.isoformat(), "warm_up_days": WARM_UP_DAYS}
        names = ["spy_1m", "spy_5m", "spy_quotes", "earnings", "earnings:USA:SPY",
                 "earnings:USA:AAPL", "borrow", "etf_daily", "energy_daily", "universe:spy_top2",
                 "universe:energy_top3", "schedule:chicago_1002"]
        self.tallies = {name: Tally() for name in names}
        self.indicator_tallies = {i.name: Tally() for i in INDICATORS}
        self.ready = {i.name: False for i in INDICATORS}
        self.first_ready = {i.name: None for i in INDICATORS}
        self.values_seen: set[tuple] = set()  # (indicator, instrument, value end time)
        self.membership = {"spy_top2": {"joined": 0, "left": 0, "joined_during_warm_up": 0},
                           "energy_top3": {"joined": 0, "left": 0, "joined_during_warm_up": 0}}
        self.members = {"spy_top2": set(), "energy_top3": set()}
        self.timings: dict[str, int] = {}  # earnings listings by report timing
        self.report_dates: set[str] = set()  # (instrument, report date) pairs listed
        self.quote_gaps = 0  # quote entries without a bid and an ask
        self.warm_up_mismatches = 0  # callbacks whose ctx.warming_up disagrees with the clock
        self.schedule_weekend_calls = 0  # scheduled calls on a Saturday or Sunday in Chicago
        self.schedule_local = set()  # wall times of scheduled calls in Chicago
        self.evaluations = {"warming_up": 0, "live": 0}
        # Borrow terms: the New York sessions delivered, records per session and instrument,
        # records with a quantity, records stamped on another date than their delivery's
        # session, and scheduled calls that ran before their session's terms arrived.
        self.borrow_sessions: set[str] = set()
        self.borrow_seen: set[tuple] = set()
        self.borrow = {"records": 0, "with_quantity": 0, "repeated": 0,
                       "stamped_off_session": 0, "schedule_before_terms": 0}

    def _replay(self, ctx) -> bool:
        """ctx.warming_up, after counting a disagreement with the clock rule."""
        if ctx.warming_up != (ctx.time < self.live_from):
            self.warm_up_mismatches += 1
        return ctx.warming_up

    def update(self, ctx, data, views):
        warming = self._replay(ctx)
        self.evaluations["warming_up" if warming else "live"] += 1
        for name in ("spy_1m", "spy_5m", "etf_daily", "energy_daily"):
            for bar in data.series(name).values():
                self.tallies[name].add(bar.end, warming)
        for quote in data.quotes.values():
            if quote is None:
                self.quote_gaps += 1
                continue
            self.tallies["spy_quotes"].add(quote.end, warming)
        for listing in data.custom.get("earnings", ()):
            self.tallies["earnings"].add(listing.time, warming)
            self.timings[listing.timing] = self.timings.get(listing.timing, 0) + 1
            self.report_dates.add(f"{listing.instrument.value} {listing.report_date}")
            per = self.tallies.get(f"earnings:{listing.instrument.value}")
            if per is not None:
                per.add(listing.time, warming)
        for record in data.custom.get("borrow", ()):
            session = ctx.time.astimezone(NEW_YORK).date().isoformat()
            self.tallies["borrow"].add(record.time, warming)
            self.borrow_sessions.add(session)
            self.borrow["records"] += 1
            self.borrow["with_quantity"] += record.shortable is not None
            self.borrow["stamped_off_session"] += (
                record.time.astimezone(NEW_YORK).date().isoformat() != session)
            key = (session, record.instrument)
            self.borrow["repeated"] += key in self.borrow_seen
            self.borrow_seen.add(key)
        for declaration in INDICATORS:
            universe = declaration.selection.universe
            instruments = ctx.members(universe) if universe else declaration.selection.instruments
            tally = self.indicator_tallies[declaration.name]
            for instrument in instruments:
                reading = ctx.indicator(declaration.name, instrument)
                seen = (declaration.name, instrument, reading.time)
                if reading.time is not None and seen not in self.values_seen:
                    self.values_seen.add(seen)
                    tally.add(reading.time, warming)
                if reading.is_ready and not self.ready[declaration.name]:
                    self.ready[declaration.name] = True
                    self.first_ready[declaration.name] = ctx.time.isoformat()
        return None

    def on_securities_changed(self, ctx, added, removed):
        # The runner passes no universe name; each universe's change is the difference
        # between its members now and at the previous call.
        replay = self._replay(ctx)
        for universe, previous in self.members.items():
            now = set(ctx.members(universe))
            joined, left = now - previous, previous - now
            self.members[universe] = now
            if not joined and not left:
                continue
            self.tallies[f"universe:{universe}"].add(ctx.time, replay, len(joined) + len(left))
            counts = self.membership[universe]
            counts["joined"] += len(joined)
            counts["left"] += len(left)
            counts["joined_during_warm_up"] += len(joined) if replay else 0
        return None

    def on_scheduled(self, ctx, name, views):
        self.tallies[f"schedule:{name}"].add(ctx.time, self._replay(ctx))
        if ctx.time.astimezone(NEW_YORK).date().isoformat() not in self.borrow_sessions:
            self.borrow["schedule_before_terms"] += 1
        local = ctx.time.astimezone(ZoneInfo(SCHEDULE.timezone))
        self.schedule_local.add(local.strftime("%H:%M"))
        self.schedule_weekend_calls += local.weekday() >= 5
        return None


def summarize(probe: CoverageProbe) -> dict:
    """The per-declaration counts the report records, logs and asserts on."""
    return {
        "inputs": {name: t.summary() for name, t in probe.tallies.items()},
        "indicators": {
            name: {**t.summary(), "ready": probe.ready[name],
                   "first_ready": probe.first_ready[name]}
            for name, t in probe.indicator_tallies.items()
        },
        "membership": probe.membership,
        "earnings_timings": probe.timings,
        "earnings_report_dates": sorted(probe.report_dates),
        "quote_entries_without_bid_and_ask": probe.quote_gaps,
        "schedule_local_times": sorted(probe.schedule_local),
        "schedule_weekend_calls": probe.schedule_weekend_calls,
        "warm_up_mismatches": probe.warm_up_mismatches,
        "evaluations": probe.evaluations,
        "borrow": {**probe.borrow, "sessions": len(probe.borrow_sessions)},
    }


def missing(summary: dict) -> list[str]:
    """Declarations with no arrival. Per-instrument earnings tallies are informative only:
    SPY, an ETF, reports no earnings."""
    names = [n for n, s in summary["inputs"].items()
             if not n.startswith("earnings:") and s["arrivals"] == 0]
    names += [n for n, s in summary["indicators"].items() if s["arrivals"] == 0]
    return names


def log_line(summary: dict, fills: list[dict]) -> str:
    """One compact log line.

    Per input: [arrivals, distinct times, arrivals during warm-up, first time]; per
    indicator: [value updates observed, distinct value end times, ready at the end].
    """
    compact = {
        "inputs": {n: [s["arrivals"], s["distinct_times"], s["during_warm_up"], s["first"]]
                   for n, s in summary["inputs"].items()},
        "indicators": {n: [s["arrivals"], s["distinct_times"], s["ready"]]
                       for n, s in summary["indicators"].items()},
        "membership": summary["membership"],
        "earnings_timings": summary["earnings_timings"],
        "earnings_report_dates": summary["earnings_report_dates"],
        "quote_gaps": summary["quote_entries_without_bid_and_ask"],
        "schedule_local_times": summary["schedule_local_times"],
        "schedule_weekend_calls": summary["schedule_weekend_calls"],
        "warm_up_mismatches": summary["warm_up_mismatches"],
        "evaluations": summary["evaluations"],
        "borrow": summary["borrow"],
        # Each fill: what, the clock, the callback that performed it; the first
        # LOGGED_FILLS of them, so a large universe keeps the line short.
        "fill_count": len(fills),
        "fills": [[f["what"], f["clock"], f["callback"]] for f in fills[:LOGGED_FILLS]],
    }
    return "COVERAGE_CHECK " + json.dumps(compact, sort_keys=True, separators=(",", ":"))


def _report(context):
    probe = context.runner.graph.nodes["probe"]
    adapter = context.runner.adapter
    summary = summarize(probe)
    fills = list(getattr(adapter, "fills", ()))
    summary["fills"] = fills
    context.record("coverage_check", summary)
    # Run output is read from the backtest log. LEAN keeps
    # no log message of the final step when on_end_of_algorithm raises, so a failing
    # assertion carries the same line in its message, which the runtime error reports.
    line = log_line(summary, fills)
    algorithm = getattr(adapter, "algorithm", None)
    if algorithm is not None:
        algorithm.log(line)
    empty = missing(summary)
    assert not empty, f"declarations with no arrival: {', '.join(empty)}; {line}"
    # Every callback's ctx.warming_up matches the clock, and the schedule keeps to trading days.
    assert summary["warm_up_mismatches"] == 0, f"ctx.warming_up disagrees with the clock; {line}"
    assert summary["schedule_weekend_calls"] == 0, f"scheduled calls on weekends; {line}"
    # Borrow terms arrive once per session and instrument, stamped in that session, before
    # the session's scheduled call (QuantConnectAdapter._deliver_borrow).
    borrow = summary["borrow"]
    assert borrow["repeated"] == borrow["stamped_off_session"] == 0, f"borrow terms; {line}"
    assert borrow["schedule_before_terms"] == 0, f"schedule before borrow terms; {line}"
    return summary


def deployment(parameter):
    """Coverage check. Parameters: start_date, end_date."""
    environment = Environment(
        start=date.fromisoformat(parameter("start_date") or "2025-07-21"),
        end=date.fromisoformat(parameter("end_date") or "2025-08-01"),
        borrow_data="interactive_brokers",
    )

    def build():
        probe = CoverageProbe(environment.start)
        return Graph().add_node("probe", probe).set_output("probe", "views").compile()

    window = {"window": [str(environment.start), str(environment.end)]}
    return Deployment("coverage-check", window, build, {SPY: "SPY", AAPL: "AAPL"},
                      environment=environment, report=_report)
