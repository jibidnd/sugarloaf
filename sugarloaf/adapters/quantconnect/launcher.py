"""Platform initialization and end-of-run reporting for any configured strategy.

A run's output goes to the backtest log and to runtime statistics, which the caller reads
through QuantConnect's REST API (``cloud.backtests.read_run_output``) and saves on disk.
The Object Store holds persistent things only (fitted models, datasets reused across
projects); ``stored_reader`` reads them, and nothing here writes to it.

``finish`` writes these log lines, each ``<TAG> <part>/<parts> <json text>``: a JSON value
longer than one line is split across consecutive lines, because QuantConnect keeps at most
8,192 characters of a log line, timestamp included, and drops the rest without error.

* ``RUN_SUMMARY``: name, mode, environment, execution policy, ``runner.finish()``, member
  counts per universe, order and update counts, node configs, the strategy report's return
  value and its records (``ReportContext.record(label, value)``), and the error list.
* ``VIEW_METRICS``: ``sugarloaf.research.view_metrics.view_metrics`` of the forecast archive.

The two character budgets below keep one run's lines well under the organization's log quota
per backtest (100 KB on the Quant Researcher tier; see docs/technical/RUNTIME.md). Raise
them for a tier with a larger quota.
"""

import inspect
import json
import platform
from collections import defaultdict

import numpy
import scipy

from sugarloaf import __version__
from sugarloaf.adapters.quantconnect.adapter import QuantConnectAdapter, Ticker
from sugarloaf.core.inputs import RESOLUTION_PERIODS, HistoryRequest
from sugarloaf.deployment.reporting import ReportContext, serialize
from sugarloaf.research.view_metrics import (
    PriceTable,
    compact,
    headline,
    price_requests,
    reduced,
    view_metrics,
)
from sugarloaf.runtime import GraphRunner

# Characters of one log line's message; LEAN adds a 20-character timestamp and QuantConnect
# keeps 8,192 characters of the whole line.
LINE_CHARACTERS = 8000
# Character budgets of each kind of line, summed over its parts and values.
SUMMARY_BUDGET = 10_000
METRICS_BUDGET = 32_000
# The price series an outcome reads: a price return from split-adjusted closes, a total
# return from closes adjusted for splits and dividends.
CONVENTION_NORMALIZATION = {"price_return": "split_adjusted", "total_return": "adjusted"}
RESOLUTION_NAMES = {period: name for name, period in RESOLUTION_PERIODS.items()}
# Instruments per history call when reading outcome prices.
HISTORY_CHUNK = 50


def stored_reader(algorithm):
    """Read-only access to files the account keeps on the platform (QC's Object Store)."""
    store = algorithm.object_store

    def read(key: str) -> str | None:
        return store.read(key) if store.contains_key(key) else None

    return read


def fetcher(algorithm):
    """HTTP GET through the platform: the response text, or None when nothing came back."""

    def fetch(url: str, headers: dict[str, str] | None = None) -> str | None:
        text = algorithm.download(url, headers or {})
        return text or None

    return fetch


def load(factory, algorithm):
    """Call a deployment factory with the native parameters, and platform access if it asks."""
    wanted = inspect.signature(factory).parameters
    extra = {}
    if "stored" in wanted:
        extra["stored"] = stored_reader(algorithm)
    if "fetch" in wanted:
        extra["fetch"] = fetcher(algorithm)
    return factory(algorithm.get_parameter, **extra)


def start(algorithm: object, deployment: object) -> QuantConnectAdapter:
    """Apply deployment settings, build the adapter and the runner, set up declarations.

    Returns:
        The ``QuantConnectAdapter`` whose callbacks the algorithm forwards.
    """
    from AlgorithmImports import AccountType, BrokerageName

    env = deployment.environment
    algorithm.set_start_date(env.start.year, env.start.month, env.start.day)
    algorithm.set_end_date(env.end.year, env.end.month, env.end.day)
    algorithm.set_time_zone(env.timezone)
    algorithm.set_account_currency(env.currency)
    algorithm.set_cash(env.cash)
    algorithm.set_brokerage_model(
        getattr(BrokerageName, env.brokerage), getattr(AccountType, env.account_type)
    )
    algorithm.set_benchmark(env.benchmark)
    tickers = {
        instrument: binding if isinstance(binding, Ticker) else Ticker(binding)
        for instrument, binding in deployment.tickers.items()
    }
    adapter = QuantConnectAdapter(
        algorithm, tickers, slippage=env.slippage, leverage=env.leverage,
        borrow_data=env.borrow_data,
    )
    runner = GraphRunner(
        deployment.graph_factory(), adapter,
        constraints=deployment.constraints, policy=deployment.execution,
    )
    adapter.runner = runner
    runner.start()
    algorithm.debug(
        "SUGARLOAF_RUN "
        + json.dumps(
            {
                "name": deployment.name,
                "mode": "trading" if runner.trading else "forecast",
                "sugarloaf": __version__,
                "python": platform.python_version(),
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
            },
            sort_keys=True,
        )
    )
    return adapter


def log_lines(tag: str, value) -> list[str]:
    """``value`` as JSON in lines ``<tag> <part>/<parts> <text>`` of at most
    ``LINE_CHARACTERS`` characters; ``cloud.backtests.parse_run_output`` joins them."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    width = LINE_CHARACTERS - len(tag) - 16
    chunks = [text[i : i + width] for i in range(0, len(text), width)] or [""]
    return [f"{tag} {k}/{len(chunks)} {chunk}" for k, chunk in enumerate(chunks, 1)]


def _size(lines: list[str]) -> int:
    return sum(len(line) for line in lines)


def outcome_prices(adapter, records, errors: list[str]) -> PriceTable:
    """Closes of every instrument the views' outcomes read, from ``adapter.history``.

    Each instrument is read at its subscription's resolution (``adapter.periods``) from
    before its earliest view to the clock, which at the end of the run is past every
    horizon that can be scored. An instrument without a subscription, or a convention
    without a price series, gets no closes; its views count as having no bar.
    """
    now = adapter.time
    groups: dict = defaultdict(dict)
    for (convention, instrument), (start, _end) in price_requests(records).items():
        normalization = CONVENTION_NORMALIZATION.get(convention)
        resolution = RESOLUTION_NAMES.get(adapter.periods.get(instrument))
        if normalization is None or resolution is None:
            continue
        groups[(convention, resolution)][instrument] = start
    bars: dict = {}
    for (convention, resolution), starts in groups.items():
        items = sorted(starts.items(), key=lambda item: item[0].value)
        for offset in range(0, len(items), HISTORY_CHUNK):
            part = items[offset : offset + HISTORY_CHUNK]
            # Two bar periods of margin keep the bar ending at the earliest start.
            span = now - min(start for _, start in part) + 2 * RESOLUTION_PERIODS[resolution]
            request = HistoryRequest(
                tuple(instrument for instrument, _ in part), span=span,
                resolution=resolution, normalization=CONVENTION_NORMALIZATION[convention],
            )
            try:
                history = adapter.history(request)
            except Exception as error:
                errors.append(f"history {convention} {resolution}: {type(error).__name__}: "
                              f"{str(error)[:200]}")
                continue
            for instrument, series in history.items():
                bars.setdefault(convention, {})[instrument] = [(b.end, b.close) for b in series]
    return PriceTable(bars)


def run_view_metrics(adapter, runner, errors: list[str]) -> dict:
    """The archive's view metrics at the adapter's clock, or a count-only record on error."""
    records = list(runner.archive)
    try:
        prices = outcome_prices(adapter, records, errors)
        return view_metrics(records, prices, adapter.time)
    except Exception as error:
        errors.append(f"view metrics: {type(error).__name__}: {str(error)[:300]}")
        return {"as_of": adapter.time.isoformat(), "counts": {"issued": len(records)},
                "kinds": {}}


def output_lines(algorithm, deployment, adapter, *, strategy, records, errors) -> tuple:
    """Build every output line of the run; returns ``(lines, metrics)``."""
    runner = adapter.runner
    metrics = run_view_metrics(adapter, runner, errors)
    metric_lines = []
    for level in range(3):
        metric_lines = log_lines("VIEW_METRICS", reduced(metrics, level))
        if _size(metric_lines) <= METRICS_BUDGET:
            if level:
                errors.append(f"VIEW_METRICS: detail level {level} to fit "
                              f"the {METRICS_BUDGET}-character budget")
            break
    else:
        errors.append("VIEW_METRICS: pooled metrics exceed the budget; counts only")
        metric_lines = log_lines("VIEW_METRICS", {"as_of": metrics.get("as_of"),
                                                  "counts": metrics.get("counts")})
    summary = {
        "name": deployment.name,
        "mode": "trading" if runner.trading else "forecast",
        "backtest": {"project_id": getattr(algorithm, "project_id", None),
                     "algorithm_id": getattr(algorithm, "algorithm_id", None)},
        "environment": serialize(deployment.environment),
        "execution": serialize(deployment.execution),
        "run": runner.finish(),
        "members": {name: len(members) for name, members in adapter.memberships.items()},
        "orders": len(runner.orders),
        "updates": len(runner.updates),
        "nodes": {
            node_id: serialize(getattr(node, "config", {}))
            for node_id, node in runner.graph.nodes.items()
        },
        "strategy": serialize(strategy),
        "records": {r["label"]: r["value"] for r in records},
        "errors": errors,
    }
    summary = compact(json.loads(json.dumps(summary, default=str)))
    summary_lines = log_lines("RUN_SUMMARY", summary)
    # Bulky optional parts leave first; the error list says which left.
    for key in ("strategy", "nodes", "records", "environment", "execution"):
        if _size(summary_lines) <= SUMMARY_BUDGET:
            break
        summary[key] = None
        summary["errors"] = [*summary["errors"], f"RUN_SUMMARY: {key} omitted for size"]
        summary_lines = log_lines("RUN_SUMMARY", summary)
    return summary_lines + metric_lines, metrics


def finish(algorithm, deployment, adapter):
    """Write the run's output to the backtest log and its headline to runtime statistics.

    Runs from ``SugarloafAlgorithm.on_end_of_algorithm``, at the platform's last clock, so
    every horizon that has ended is behind it. The strategy report runs first; an exception
    it raises is recorded, the output is written, and the exception is raised again (LEAN
    then keeps the error message, not the final step's log lines).
    """
    runner = adapter.runner
    errors: list[str] = []
    records: list[dict] = []

    def record(label, value):
        records.append({"label": label, "value": serialize(value)})

    failure = None
    strategy = {}
    if deployment.report:
        try:
            strategy = deployment.report(ReportContext(runner, record)) or {}
        except Exception as error:
            failure = error
            errors.append(f"report: {type(error).__name__}: {str(error)[:300]}")
    lines, metrics = output_lines(
        algorithm, deployment, adapter, strategy=strategy, records=records, errors=errors
    )
    for line in lines:
        algorithm.log(line)
    for name, value in headline(metrics).items():
        algorithm.set_runtime_statistic(name, value)
    if failure is not None:
        raise failure
