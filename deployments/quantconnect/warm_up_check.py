"""Native check of windows and indicators under a WarmUp declaration.

The node declares daily windows of SPY (the security's own daily feed) and QQQ (a minute
feed consolidated to the exchange session), an EMA on each, and a WarmUp of 45 calendar
days. The adapter fills each window from one history call and warms each indicator with
``warm_up_indicator`` on first touch, ending before the bar or clock in hand at the start
of the warm-up replay; the replay then delivers the sessions up to the start date.

The report counts, for every window and indicator, the values held and the distinct end
times among them: equal counts mean each bar period is held once. It counts the evaluations
at which a window's or indicator's newest end time is later than ``ctx.time`` (zero means
no value from the future), records each fill with the callback that performed it, and the
first bar delivered after warm-up and whether it heads its window.
The graph's output port emits views, so this is a forecast run: nothing is traded.

    sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/warm_up_check.toml
"""

import json
from datetime import date, timedelta

from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import Bars, Indicator, Selection, WarmUp
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.views import ScoreView
from sugarloaf.deployment import Deployment, Environment

SPY, QQQ = InstrumentId("USA:SPY"), InstrumentId("USA:QQQ")
WINDOW = 60  # more sessions than the warm-up replays, so a repeated session stays visible


def _window_counts(window) -> dict:
    """Bars held, distinct end times, and whether end times strictly fall from index 0."""
    ends = [bar.end for bar in window]
    return {
        "bars": len(ends),
        "distinct_end_times": len(set(ends)),
        "newest_first": all(a > b for a, b in zip(ends, ends[1:])),
        "newest": ends[0].isoformat() if ends else None,
        "oldest": ends[-1].isoformat() if ends else None,
    }


def _indicator_counts(reading) -> dict:
    times = [time for time, _ in reading.values]
    return {
        "values": len(times),
        "distinct_end_times": len(set(times)),
        "newest_first": all(a > b for a, b in zip(times, times[1:])),
        "value": reading.value,
    }


class WarmUpProbe:
    """At each delivered bar: measure every window and indicator; emit nothing."""

    ports = {}
    outputs = {"views": OutputSpec((ScoreView,))}
    inputs = (
        # SPY: subscribed daily; bars go to the window as LEAN delivers them.
        Bars(Selection((SPY,)), "daily", window=WINDOW),
        # QQQ: subscribed at minute; a session consolidator builds the daily bars.
        Bars(Selection((QQQ,)), "minute", interval=timedelta(days=1), window=WINDOW),
    )
    indicators = (
        # Built by LEAN's ema helper at the daily resolution and warmed automatically.
        Indicator("spy_ema", "ema", 10, Selection((SPY,)), "daily", window=WINDOW),
        # Built by class, registered with a session consolidator, warmed by warm_up_indicator.
        Indicator("qqq_ema", "ema", 10, Selection((QQQ,)), "minute", timedelta(days=1),
                  window=WINDOW),
    )
    warm_up = WarmUp(span=timedelta(days=45))

    def __init__(self):
        self.config = {"window": WINDOW, "warm_up_days": 45}
        self.latest = {}
        self.unequal = {}  # evaluations at which a count and its distinct count differed
        self.ahead = {}  # evaluations at which the newest end time is later than ctx.time
        self.evaluations = {"warming_up": 0, "live": 0}
        self.replayed = {"SPY": 0, "QQQ": 0}  # bars delivered in warm-up slices
        self.first_live = {}

    def update(self, ctx, data, views):
        self.evaluations["warming_up" if ctx.warming_up else "live"] += 1
        for label, instrument, feed, indicator in (
            ("SPY", SPY, self.inputs[0], "spy_ema"),
            ("QQQ", QQQ, self.inputs[1], "qqq_ema"),
        ):
            bar = data.bars.get(instrument)
            window = ctx.window(feed, instrument)
            if bar is not None and ctx.warming_up:
                self.replayed[label] += 1
            if bar is not None and not ctx.warming_up and label not in self.first_live:
                # The first bar after warm-up must reach the window, at index 0.
                self.first_live[label] = {
                    "end": bar.end.isoformat(),
                    "heads_window": len(window) > 0 and window[0].end == bar.end,
                }
            counts = {
                "window": _window_counts(window),
                "indicator": _indicator_counts(ctx.indicator(indicator, instrument)),
            }
            reading = ctx.indicator(indicator, instrument)
            newest = {
                "window": window[0].end if len(window) else None,
                "indicator": reading.values[0][0] if reading.values else None,
            }
            for part, key in (("window", "bars"), ("indicator", "values")):
                name = f"{label}:{part}"
                if counts[part][key] != counts[part]["distinct_end_times"]:
                    self.unequal[name] = self.unequal.get(name, 0) + 1
                self.ahead.setdefault(name, 0)
                if newest[part] is not None and newest[part] > ctx.time:
                    self.ahead[name] += 1
            self.latest[label] = counts
        return None


def _report(context):
    probe = context.runner.graph.nodes["probe"]
    summary = {
        "final": probe.latest,
        "evaluations_with_repeated_end_times": probe.unequal,
        "evaluations_with_end_time_after_ctx_time": probe.ahead,
        "evaluations": probe.evaluations,
        "bars_delivered_while_warming_up": probe.replayed,
        "first_live_bar": probe.first_live,
    }
    context.record("warm_up_check", summary)
    # The same counts in the backtest log, readable with `sugarloaf logs` where the
    # account cannot export from the Object Store.
    counts = {
        label: [parts["window"]["bars"], parts["window"]["distinct_end_times"],
                parts["indicator"]["values"], parts["indicator"]["distinct_end_times"]]
        for label, parts in probe.latest.items()
    }
    adapter = context.runner.adapter
    # Each history fill and indicator warm-up: what, the clock, the cutoff, the callback.
    summary["fills"] = list(getattr(adapter, "fills", ()))
    algorithm = getattr(adapter, "algorithm", None)
    if algorithm is not None:
        algorithm.log("WARM_UP_CHECK " + json.dumps({
            "window_bars_distinct_indicator_values_distinct": counts,
            "evaluations_with_repeated_end_times": probe.unequal,
            "evaluations_with_end_time_after_ctx_time": probe.ahead,
            "evaluations": probe.evaluations,
            "fills": summary["fills"],
            "bars_delivered_while_warming_up": probe.replayed,
            "first_live_bar": probe.first_live,
        }, sort_keys=True))
    return summary


def deployment(parameter):
    """Warm-up check. Parameters: start_date, end_date."""

    def build():
        return Graph().add_node("probe", WarmUpProbe()).set_output("probe", "views").compile()

    environment = Environment(
        start=date.fromisoformat(parameter("start_date") or "2025-01-06"),
        end=date.fromisoformat(parameter("end_date") or "2025-01-10"),
    )
    window = {"window": [str(environment.start), str(environment.end)]}
    return Deployment("warm-up-check", window, build, {SPY: "SPY", QQQ: "QQQ"},
                      environment=environment, report=_report)
