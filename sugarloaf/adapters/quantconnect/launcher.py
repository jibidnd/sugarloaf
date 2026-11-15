"""Platform initialization and end-of-run reporting for any configured strategy."""

import inspect
import json
import platform

import numpy
import scipy

from sugarloaf import __version__
from sugarloaf.adapters.quantconnect.adapter import QuantConnectAdapter, Ticker
from sugarloaf.deployment.reporting import ReportContext, serialize
from sugarloaf.research.serialization import encode_archive
from sugarloaf.runtime import GraphRunner

RESULT_PREFIX = "sugarloaf/results"


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


def finish(algorithm, deployment, adapter):
    """Save the run's result record (and forecast archive) to the Object Store."""
    runner = adapter.runner
    records = []

    def record(label, value):
        records.append({"label": label, "value": serialize(value)})

    context = ReportContext(runner, record)
    strategy = deployment.report(context) if deployment.report else {}
    result = {
        "name": deployment.name,
        "mode": "trading" if runner.trading else "forecast",
        "environment": serialize(deployment.environment),
        "execution": serialize(deployment.execution),
        "run": runner.finish(),
        "members": {
            name: [i.value for i in members] for name, members in adapter.memberships.items()
        },
        "orders": [serialize(o) for o in runner.orders[-5000:]],
        "updates": [serialize(u) for u in runner.updates[-5000:]],
        "strategy": serialize(strategy),
        "records": records,
        "nodes": {
            node_id: serialize(getattr(node, "config", {}))
            for node_id, node in runner.graph.nodes.items()
        },
    }
    key = f"{RESULT_PREFIX}/{algorithm.project_id}/{algorithm.algorithm_id}"
    adapter.save(key + "/result.json", json.dumps(result, default=str, sort_keys=True))
    if not runner.trading:
        adapter.save(key + "/forecasts.json", json.dumps(encode_archive(runner.archive)))
    summary = {
        "key": key,
        "steps": runner.steps,
        "orders": len(runner.orders),
        "forecasts": len(runner.archive),
        "diagnostics": runner.diagnostic_count,
    }
    algorithm.debug("SUGARLOAF_RESULT " + json.dumps(summary, sort_keys=True))
