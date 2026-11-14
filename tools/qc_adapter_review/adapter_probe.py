"""Where the adapter spends its time for the move-size node's declarations.

A probe node declares what Sugarloaf Lab's move-size node declares (the 200-stock yearly
liquidity panel, 4,900 adjusted daily bars with ``require_all=False``, the 14 funds, the VIX
as a raw index, fundamentals, 6,800 days of earnings listings, instrument metadata) and at
each SPY close reads all of it once and holds nothing. It fits no model, so its evaluation
time is the adapter's read cost, which the node records per session by kind of read.

The factory also profiles the host: the construction of ``QuantConnectHost`` (the explicit
instruments' prefill), the first data callback (the universe members' bulk prefill and the
first evaluation, which loads the earnings calendar) and the last data callback (steady
state). The ``cProfile`` statistics go out as report records at the end of the run, in
pieces that fit the platform's log line. The profiling hooks wrap the host class's methods
from this module; the adapter itself is not changed.

    uv run --cache-dir .uv-cache sugarloaf run --adapter quantconnect \\
        --manifest tools/qc_adapter_review/adapter_probe.toml

Parameters: start_date, end_date, bars_lookback, earnings_days, profile ("on"/"off").
"""

import cProfile
import io
import pstats
import time
from datetime import date, timedelta

from sugarloaf.adapters.quantconnect import host as qc_host
from sugarloaf.core.errors import CapabilityError, NotReady
from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.deployment import Deployment, Environment
from sugarloaf.market.contracts import (
    BarsRequirement,
    EarningsRequirement,
    FundamentalsRequirement,
    Instrument,
    InstrumentRequirement,
    LiquidityUniverse,
    Selection,
    Trigger,
    UniverseRequirement,
)

DAY = timedelta(days=1)
VIX = InstrumentId("USA:VIX")
FUNDS = tuple(InstrumentId(f"USA:{t}") for t in (
    "SPY", "QQQ", "IWM", "DIA", "RSP", "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV",
    "XLY"))
# The move-size node's panel definition, kept identical so the adapter does the same work.
PANEL = LiquidityUniverse("size-panel-200", 200, min_price=5, require_fundamentals=True,
                          cadence="yearly")
LINE = 5500  # characters per report record, under the platform's 8,192-character log line


class LoadProbe:
    """Read everything the move-size node reads, once per SPY close; hold nothing."""

    inputs = {}
    outputs = {"positions": OutputSpec((PositionView,))}

    def __init__(self, bars_lookback=4900, earnings_days=6800):
        self.clock = FUNDS[0]
        self.panel = Selection(universe=PANEL.name)
        history = {"lookback": bars_lookback, "min_lookback": 260, "normalization": "adjusted",
                   "require_all": False}
        self.panel_bars = BarsRequirement(self.panel, DAY, **history)
        self.fund_bars = BarsRequirement(Selection(FUNDS), DAY, **history)
        self.vix_bars = BarsRequirement(Selection((VIX,)), DAY, lookback=bars_lookback,
                                        min_lookback=260, normalization="raw")
        self.fundamentals = FundamentalsRequirement(self.panel, max_age=timedelta(days=10))
        self.earnings = EarningsRequirement(self.panel, lookback=timedelta(days=earnings_days))
        self.requirements = (
            UniverseRequirement(PANEL), self.panel_bars, self.fund_bars, self.vix_bars,
            self.fundamentals, self.earnings, InstrumentRequirement(self.panel),
            InstrumentRequirement(Selection(FUNDS)))
        self.triggers = (Trigger("bar_close", self.clock, DAY),)
        # Per session: date, then seconds for the readiness scan, the bar reads, the
        # fundamentals reads, the earnings reads and the whole evaluation, then counts of
        # ready members, bars read and earnings records read.
        self.sessions = []

    def evaluate(self, inputs, ctx):
        began = time.perf_counter()
        t = time.perf_counter()
        ready = ctx.market.ready(self.panel_bars)
        funds = ctx.market.ready(self.fund_bars)
        readiness = time.perf_counter() - t
        t = time.perf_counter()
        bars = 0
        for instrument in ready:
            bars += len(ctx.market.bars(self.panel_bars, instrument))
        for instrument in funds:
            bars += len(ctx.market.bars(self.fund_bars, instrument))
        bars += len(ctx.market.bars(self.vix_bars, VIX))
        reads = time.perf_counter() - t
        t = time.perf_counter()
        sectors = 0
        for instrument in ready:
            try:
                sectors += (ctx.market.fundamentals(self.fundamentals, instrument).sector or 0) > 0
            except (NotReady, CapabilityError):
                pass
        fundamentals = time.perf_counter() - t
        t = time.perf_counter()
        listings = 0
        for instrument in ready:
            listings += len(ctx.market.earnings(self.earnings, instrument))
        earnings = time.perf_counter() - t
        end = ctx.instruments.session_close(self.clock, ctx.now, 1)
        self.sessions.append((ctx.now.date().isoformat(), round(readiness, 2), round(reads, 2),
                              round(fundamentals, 2), round(earnings, 2),
                              round(time.perf_counter() - began, 2), len(ready), bars, listings))
        metadata = Metadata(f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now, end)
        return NodeOutputs({"positions": (PositionView({}, "weight", "probe", metadata),)})


# ---- host profiling -------------------------------------------------------------------------

CALLS = []      # (utc time, bars in the slice, seconds) for every data callback
PROFILES = {}   # "construction", "first" and "steady": cProfile.Profile objects
_installed = False


def install_hooks(profile):
    """Wrap the host's constructor and data callback with timers and profilers."""
    global _installed
    if _installed:
        return
    _installed = True
    original_init = qc_host.QuantConnectHost.__init__
    original_on_data = qc_host.QuantConnectHost.on_data

    def timed_init(self, *args, **kwargs):
        began = time.perf_counter()
        profiler = cProfile.Profile() if profile else None
        if profiler:
            profiler.enable()
        try:
            original_init(self, *args, **kwargs)
        finally:
            if profiler:
                profiler.disable()
                PROFILES["construction"] = profiler
            CALLS.append(("construction", 0, round(time.perf_counter() - began, 2)))

    def timed_on_data(self, data):
        count = data.bars.count
        began = time.perf_counter()
        profiler = cProfile.Profile() if profile and count else None
        if profiler:
            profiler.enable()
        try:
            original_on_data(self, data)
        finally:
            if profiler:
                profiler.disable()
                # The first data callback carries the members' prefill and the first
                # evaluation; the latest one is the steady state at the end of the run.
                PROFILES.setdefault("first", profiler)
                PROFILES["steady"] = profiler
            CALLS.append((str(self.algorithm.utc_time), count,
                          round(time.perf_counter() - began, 2)))

    qc_host.QuantConnectHost.__init__ = timed_init
    qc_host.QuantConnectHost.on_data = timed_on_data


def profile_text(profiler, order, lines):
    """pstats text for one profile, sorted by ``order`` ("cumulative" or "tottime")."""
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.strip_dirs().sort_stats(order).print_stats(lines)
    text = stream.getvalue()
    # Drop pstats' header lines; keep the table.
    table = text[text.find("ncalls"):] if "ncalls" in text else text
    return table


# ---- deployment -----------------------------------------------------------------------------

def deployment(parameter):
    """Probe deployment. Parameters: start_date, end_date, bars_lookback, earnings_days,
    profile ("on" by default)."""
    bars = int(parameter("bars_lookback") or 4900)
    days = int(parameter("earnings_days") or 6800)
    profile = (parameter("profile") or "on") == "on"
    install_hooks(profile)
    node = LoadProbe(bars_lookback=bars, earnings_days=days)

    def build():
        return Graph().add_node("probe", node).set_output("probe", "positions").compile()

    def report(context):
        out = {"sessions": len(node.sessions), "calls": len(CALLS)}
        context.record("probe.sessions", {"columns": ["date", "readiness", "bars", "fundamentals",
                                                      "earnings", "evaluate", "ready_members",
                                                      "bars_read", "listings"],
                                          "rows": node.sessions[:60]})
        context.record("probe.calls", {"columns": ["utc", "bars_in_slice", "seconds"],
                                       "rows": CALLS[:80]})
        for name, profiler in PROFILES.items():
            for order, lines in (("cumulative", 60), ("tottime", 35)):
                text = profile_text(profiler, order, lines)
                pieces = [text[i:i + LINE] for i in range(0, len(text), LINE)]
                for index, piece in enumerate(pieces):
                    context.record(f"probe.profile.{name}.{order}",
                                   {"part": index, "parts": len(pieces), "text": piece})
        return out

    environment = Environment(
        start=date.fromisoformat(parameter("start_date") or "2021-01-04"),
        end=date.fromisoformat(parameter("end_date") or "2021-01-29"),
        cash=1_000_000,
    )
    tickers = {f: f.value.split(":")[1] for f in FUNDS}
    tickers[VIX] = Instrument(VIX, "VIX", asset_class="index")
    settings = {"bars_lookback": bars, "earnings_days": days, "profile": profile,
                "window": [str(environment.start), str(environment.end)]}
    return Deployment("qc-adapter-review-probe", settings, build, tickers, environment=environment,
                      report=report)
