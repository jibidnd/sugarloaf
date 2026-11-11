"""Native check of index data: a node reads SPY and the VIX index side by side.

The deployment declares the VIX as an index with an ``Instrument`` record; the node
only asks for daily bars of ``USA:VIX``, as it asks for ``USA:SPY``. The report says
whether every session's VIX bar arrived with the SPY bar of the same session, what the
adapter reports about each instrument, and the VIX's highest close in the window.

    sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/index_check.toml
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sugarloaf.core.graph import Graph
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.primitives import InstrumentId, Metadata
from sugarloaf.core.views import PositionView
from sugarloaf.deployment import Deployment, Environment
from sugarloaf.market.contracts import (
    BarsRequirement,
    Instrument,
    InstrumentRequirement,
    Selection,
    Trigger,
)

SPY, VIX = InstrumentId("USA:SPY"), InstrumentId("USA:VIX")
DAY = timedelta(days=1)
NEW_YORK = ZoneInfo("America/New_York")


def session(bar):
    # A daily bar's session date in New York, whichever end stamp the platform uses.
    return bar.start.astimezone(NEW_YORK).date().isoformat()


class IndexProbe:
    """Each session at the close: record the latest SPY and VIX bars; hold nothing."""

    inputs = {}
    outputs = {"positions": OutputSpec((PositionView,))}

    def __init__(self):
        # SPY adjusted for splits and dividends; the index has one raw series only.
        self.spy = BarsRequirement(Selection((SPY,)), DAY, lookback=5)
        self.vix = BarsRequirement(Selection((VIX,)), DAY, lookback=5, normalization="raw")
        self.requirements = (self.spy, self.vix, InstrumentRequirement(Selection((SPY, VIX))))
        # Evaluate once per completed SPY daily bar, at the close.
        self.triggers = (Trigger("bar_close", SPY, DAY),)
        self.rows, self.metadata = [], {}

    def evaluate(self, inputs, ctx):
        spy, vix = ctx.market.bars(self.spy, SPY)[-1], ctx.market.bars(self.vix, VIX)[-1]
        self.rows.append((ctx.now.isoformat(timespec="minutes"), session(spy), spy.close,
                          session(vix), vix.close, vix.start.isoformat(), vix.end.isoformat()))
        if not self.metadata:
            for instrument in (SPY, VIX):
                record = ctx.instruments.get(instrument)
                self.metadata[instrument.value] = {"asset_class": record.asset_class,
                                                   "tradable": record.tradable}
        metadata = Metadata(f"{ctx.node_id}:{ctx.snapshot_id}", ctx.node_id, ctx.now,
                            ctx.now + timedelta(days=4))
        return NodeOutputs({"positions": (PositionView({}, "weight", "index-check", metadata),)})


def _report(context):
    probe = context.runtime.graph.nodes["probe"]
    rows = probe.rows
    top = max(rows, key=lambda row: row[4]) if rows else None
    summary = {
        "evaluations": len(rows),
        "sessions_with_vix_of_same_session": sum(row[1] == row[3] for row in rows),
        "mismatched_rows": [row for row in rows if row[1] != row[3]][:5],
        "instruments": probe.metadata,
        "vix_highest_close": [top[3], top[4]] if top else None,
        "first_rows": rows[:3],
        "last_rows": rows[-3:],
    }
    context.record("index_check", summary)
    return summary


def deployment(parameter):
    """Index check. Parameters: start_date, end_date."""

    def build():
        return Graph().add_node("probe", IndexProbe()).set_output("probe", "positions").compile()

    environment = Environment(
        start=date.fromisoformat(parameter("start_date") or "2025-03-24"),
        end=date.fromisoformat(parameter("end_date") or "2025-04-17"),
    )
    tickers = {SPY: "SPY", VIX: Instrument(VIX, "VIX", asset_class="index")}
    window = {"window": [str(environment.start), str(environment.end)]}
    return Deployment("index-check", window, build, tickers, environment=environment,
                      report=_report)
