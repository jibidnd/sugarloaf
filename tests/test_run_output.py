"""A run's output: ``launcher.finish`` writes log lines and runtime statistics, and
``cloud.backtests`` reads them back and saves them on disk.

The algorithm here records ``log`` and ``set_runtime_statistic`` and has no Object Store,
so any write to one fails the test. The adapter serves closes from a table through
``history``, as ``QuantConnectAdapter.history`` serves LEAN's bars.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sugarloaf.adapters.quantconnect import launcher
from sugarloaf.adapters.quantconnect.cloud.backtests import (
    parse_run_output,
    read_run_output,
    save_run_output,
)
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import Score, ScoreView, return_target
from sugarloaf.research import ForecastArchive

SPY = InstrumentId("USA:SPY")
T0 = datetime(2025, 1, 6, 15, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
LEAN_PREFIX = "2025-01-10 16:00:00 "  # the timestamp LEAN puts before each log message


class Algorithm:
    project_id, algorithm_id = 37377184, "abc123"

    def __init__(self):
        self.lines, self.statistics = [], {}

    def log(self, message):
        self.lines.append(LEAN_PREFIX + message)

    def set_runtime_statistic(self, name, value):
        assert isinstance(value, str)
        self.statistics[name] = value


class Adapter:
    def __init__(self, archive, *, minutes=60, history_error=None):
        self.time = T0 + minutes * MINUTE
        self.periods = {SPY: MINUTE}
        self.memberships = {"top": (SPY,)}
        self.history_error = history_error
        self.requests = []
        self.bars = [SimpleNamespace(end=T0 + k * MINUTE, close=100 + 0.1 * k)
                     for k in range(minutes + 1)]
        nodes = {node: SimpleNamespace(config={"node": node})
                 for node in {r.node_id for r in archive}}
        self.runner = SimpleNamespace(
            archive=archive, trading=False, orders=[], updates=[],
            graph=SimpleNamespace(nodes=nodes),
            finish=lambda: {"steps": 7, "diagnostic_count": 0, "forecasts": len(archive)},
        )

    def history(self, request):
        self.requests.append(request)
        if self.history_error:
            raise self.history_error
        return {i: tuple(self.bars) for i in request.instruments}


def _archive(nodes=1, per_node=10, horizon=10):
    archive = ForecastArchive()
    for n in range(nodes):
        node = f"node{n}"
        for k in range(per_node):
            issued = T0 + (k % 40) * MINUTE
            end = issued + horizon * MINUTE
            view = ScoreView(return_target(SPY, Interval(issued, end)),
                             Score(float(k % 7 - 3), f"test:{node}:v1"),
                             Metadata(f"{node}:{k}", node, issued, end))
            archive.record(node, "views", (view,), issued)
    return archive


def _deployment(report=None):
    return SimpleNamespace(name="check", environment=None, execution=None, report=report)


def test_finish_writes_log_lines_that_read_back_and_no_object_store():
    algorithm, adapter = Algorithm(), Adapter(_archive(per_node=30))
    launcher.finish(algorithm, _deployment(), adapter)
    assert all(len(line) <= 8192 for line in algorithm.lines)
    output = parse_run_output(algorithm.lines)
    assert output["errors"] == []
    summary, metrics = output["summary"], output["metrics"]
    assert (summary["name"], summary["mode"], summary["members"]) == ("check", "forecast",
                                                                     {"top": 1})
    assert summary["errors"] == [] and summary["nodes"] == {"node0": {"node": "node0"}}
    assert metrics["counts"]["issued"] == 30 and metrics["counts"]["scored"] == 30
    assert len(output["samples"]) == launcher.SAMPLES_PER_NODE
    assert output["samples"][0]["view"]["metadata"]["view_id"] == "node0:0"
    assert algorithm.statistics["Views issued"] == "30"
    assert algorithm.statistics["Views scored"] == "30"
    assert "Score hit rate" in algorithm.statistics
    # Outcome prices are read at the subscription's resolution, split-adjusted.
    (request,) = adapter.requests
    assert (request.resolution, request.normalization) == ("minute", "split_adjusted")


def test_a_heavy_run_stays_within_the_log_budget():
    algorithm = Algorithm()
    launcher.finish(algorithm, _deployment(), Adapter(_archive(nodes=60, per_node=400)))
    budget = (launcher.SUMMARY_BUDGET + launcher.METRICS_BUDGET + launcher.SAMPLE_BUDGET
              + launcher.RECORD_BUDGET)
    written = sum(len(line) - len(LEAN_PREFIX) for line in algorithm.lines)
    assert written <= budget < 100_000
    output = parse_run_output(algorithm.lines)
    assert output["metrics"]["counts"]["issued"] == 24_000
    assert output["metrics"]["detail"] in {"by_node_headline", "pooled"}
    assert any("VIEW_SAMPLE" in error for error in output["summary"]["errors"])


def test_a_history_failure_is_reported_and_the_views_stay_counted():
    algorithm = Algorithm()
    adapter = Adapter(_archive(), history_error=RuntimeError("no data"))
    launcher.finish(algorithm, _deployment(), adapter)
    output = parse_run_output(algorithm.lines)
    assert any("history" in error for error in output["summary"]["errors"])
    counts = output["metrics"]["counts"]
    assert (counts["issued"], counts["scored"], counts["no_bar"]) == (10, 0, 10)


def test_a_failing_report_is_raised_after_the_output_is_written():
    def report(context):
        context.record("probe", {"arrivals": 0})
        raise AssertionError("declaration without arrivals")

    algorithm = Algorithm()
    with pytest.raises(AssertionError, match="without arrivals"):
        launcher.finish(algorithm, _deployment(report), Adapter(_archive()))
    output = parse_run_output(algorithm.lines)
    assert output["records"] == [{"label": "probe", "value": {"arrivals": 0}}]
    assert output["summary"]["errors"][0].startswith("report: AssertionError")


def test_a_value_longer_than_a_line_is_split_and_joined():
    value = {"text": "x" * 20_000}
    lines = launcher.log_lines("RUN_RECORD", value)
    assert len(lines) == 3 and all(len(line) <= launcher.LINE_CHARACTERS for line in lines)
    assert parse_run_output([LEAN_PREFIX + line for line in lines])["records"] == [value]


def test_broken_or_repeated_line_groups_are_reported():
    first, second = launcher.log_lines("VIEW_METRICS", {"text": "y" * 9000})
    summary = launcher.log_lines("RUN_SUMMARY", {"a": 1})[0]
    output = parse_run_output([first, summary, second, summary, summary, "unrelated"])
    assert output["summary"] == {"a": 1} and output["metrics"] is None
    assert any("1 of 2 parts" in e for e in output["errors"])
    assert any("without its earlier parts" in e for e in output["errors"])
    assert any("replaces an earlier one" in e for e in output["errors"])


def test_saving_refuses_to_replace_a_different_run(tmp_path):
    path = tmp_path / "runs" / "abc123.json"
    save_run_output({"backtest_id": "abc123", "summary": {"a": 1}}, path)
    save_run_output({"backtest_id": "abc123", "summary": {"a": 1}}, path)  # identical
    with pytest.raises(ValueError):
        save_run_output({"backtest_id": "abc123", "summary": {"a": 2}}, path)
    save_run_output({"backtest_id": "abc123", "summary": {"a": 2}}, path, overwrite=True)
    assert json.loads(path.read_text())["summary"] == {"a": 2}


def test_read_run_output_parses_the_rest_logs():
    lines = [LEAN_PREFIX + line for line in launcher.log_lines("RUN_SUMMARY", {"name": "x"})]

    def post(endpoint, payload):
        assert endpoint == "backtests/read/log"
        return {"length": len(lines), "logs": lines[payload["start"]:payload["end"]]}

    output = read_run_output(SimpleNamespace(post=post), 7, "abc123")
    assert (output["backtest_id"], output["log_lines"], output["summary"]) == (
        "abc123", 1, {"name": "x"})
