"""Native check of index data: a node reads SPY and the VIX index side by side.

The deployment binds the VIX as an index with a ``Ticker`` record; the node only
declares daily bars of ``USA:VIX``, as it declares ``USA:SPY``. The report says
whether every session's VIX bar arrived with the SPY bar of the same session and
the VIX's highest close in the window. The graph's output port emits views, so this
is a forecast run: nothing is traded and no position node is needed.

    sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/index_check.toml
"""

from datetime import date
from zoneinfo import ZoneInfo

from sugarloaf.adapters.quantconnect import Ticker
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import Bars, Selection
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.views import ScoreView
from sugarloaf.deployment import Deployment, Environment

SPY, VIX = InstrumentId("USA:SPY"), InstrumentId("USA:VIX")
NEW_YORK = ZoneInfo("America/New_York")


def session(bar):
    # A daily bar's session date in New York, whichever end stamp the platform uses.
    return bar.start.astimezone(NEW_YORK).date().isoformat()


class IndexProbe:
    """Each session at the close: record the latest SPY and VIX bars; emit nothing."""

    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}
    inputs = (
        # SPY adjusted for splits and dividends; the index has one raw series only.
        Bars(Selection((SPY,)), "daily", window=5),
        Bars(Selection((VIX,)), "daily", window=5, normalization="raw"),
    )

    def __init__(self):
        self.rows = []
        self.config = {}

    def update(self, ctx, data, views):
        if SPY not in data.bars:
            return None
        spy = data.bars[SPY]
        vix_window = ctx.window(self.inputs[1], VIX)
        vix = data.bars.get(VIX) or (vix_window[0] if len(vix_window) else None)
        self.rows.append((
            ctx.time.isoformat(timespec="minutes"), session(spy), spy.close,
            None if vix is None else session(vix), None if vix is None else vix.close,
            None if vix is None else vix.start.isoformat(),
            None if vix is None else vix.end.isoformat(),
            VIX in data.bars,
        ))
        return None


def _report(context):
    probe = context.runner.graph.nodes["probe"]
    rows = probe.rows
    with_vix = [row for row in rows if row[4] is not None]
    top = max(with_vix, key=lambda row: row[4]) if with_vix else None
    summary = {
        "evaluations": len(rows),
        "sessions_with_vix_of_same_session": sum(row[1] == row[3] for row in rows),
        "sessions_with_vix_in_the_same_slice": sum(row[7] for row in rows),
        "mismatched_rows": [row for row in rows if row[1] != row[3]][:5],
        "vix_highest_close": [top[3], top[4]] if top else None,
        "first_rows": rows[:3],
        "last_rows": rows[-3:],
    }
    context.record("index_check", summary)
    return summary


def deployment(parameter):
    """Index check. Parameters: start_date, end_date."""

    def build():
        return Graph().add_node("probe", IndexProbe()).set_output("probe", "views").compile()

    environment = Environment(
        start=date.fromisoformat(parameter("start_date") or "2025-03-24"),
        end=date.fromisoformat(parameter("end_date") or "2025-04-17"),
    )
    tickers = {SPY: "SPY", VIX: Ticker("VIX", "index")}
    window = {"window": [str(environment.start), str(environment.end)]}
    return Deployment("index-check", window, build, tickers, environment=environment,
                      report=_report)
