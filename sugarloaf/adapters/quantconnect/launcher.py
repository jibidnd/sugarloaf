"""Platform initialization and generic reports for any configured strategy."""

import inspect
import json
import platform

import numpy
import scipy

from sugarloaf import __version__
from sugarloaf.adapters.quantconnect import QuantConnectHost
from sugarloaf.deployment.reporting import ReportContext, serialize


def emit(algorithm, label, record):
    """Write a labeled, serialized JSON record to the platform debug log."""
    # Shared conversion handles dates/dataclasses; QC owns the output transport.
    # Stable labels let retained logs identify the configuration/result records.
    algorithm.debug(label + " " + json.dumps(serialize(record), default=str, sort_keys=True))


def stored_reader(algorithm):
    """Read-only access to files the account keeps on the platform (QC's Object Store).

    Returns a function of a key that gives the stored text, or None when the key is absent.
    Strategies use it for inputs that are built on the platform and may not leave it, such as
    derived tables of licensed data; nothing is written.
    """
    store = algorithm.object_store

    def read(key: str) -> str | None:
        return store.read(key) if store.contains_key(key) else None

    return read


def fetcher(algorithm):
    """HTTP GET through the platform: the response text, or None when nothing came back.

    LEAN's download returns an empty string on failure, which a caller could not tell from an
    empty page; None makes the failure explicit. QuantConnect staff have described limits of
    about 100 calls a backtest and about 20 a minute live (forum, 2020-21), so a strategy reads
    recorded responses through ``stored`` first and fetches only what is missing. Headers are
    a dict, as QuantBook research calls pass them.
    """

    def fetch(url: str, headers: dict[str, str] | None = None) -> str | None:
        text = algorithm.download(url, headers or {})
        return text or None

    return fetch


def load(factory, algorithm):
    """Call a deployment factory with the native parameters, and platform access if it asks.

    A factory takes ``parameter`` and may also take keywords ``stored`` (the reader of
    ``stored_reader``) and ``fetch`` (the getter of ``fetcher``). Factories without them are
    called as before.
    """
    wanted = inspect.signature(factory).parameters
    extra = {}
    if "stored" in wanted:
        extra["stored"] = stored_reader(algorithm)
    if "fetch" in wanted:
        extra["fetch"] = fetcher(algorithm)
    return factory(algorithm.get_parameter, **extra)


def start(algorithm, deployment):
    """Apply deployment environment settings and start a fresh graph host.

    Args:
        algorithm (QCAlgorithm): Native QCAlgorithm being initialized.
        deployment (Deployment): Graph factory, instrument mappings, environment, and safety policy.

    Returns:
        (QuantConnectHost): Host for forwarding data, securities, and order callbacks.

    Emits SUGARLOAF_RUN with configuration and runtime versions.
    """
    # Native enum imports belong here, not in strategy factories or shared config.
    from AlgorithmImports import AccountType, BrokerageName

    # Configure the environment before the host creates native resources, so
    # subscriptions observe the requested dates, currency and brokerage models.
    env = deployment.environment
    algorithm.set_start_date(env.start.year, env.start.month, env.start.day)
    algorithm.set_end_date(env.end.year, env.end.month, env.end.day)
    algorithm.set_time_zone(env.timezone)
    algorithm.set_account_currency(env.currency)
    algorithm.set_cash(env.cash)
    # Convert shared enum names at the boundary; LEAN implements the models.
    algorithm.set_brokerage_model(
        getattr(BrokerageName, env.brokerage), getattr(AccountType, env.account_type)
    )
    algorithm.set_benchmark(env.benchmark)
    # A native daily bar ends at its session's close and arrives then, not at the
    # following midnight. This is LEAN's default; it is stated because the graph
    # clock and every daily decision time depend on it.
    algorithm.settings.daily_precise_end_time = True
    # Call graph_factory once for this host: nodes and their mutable history must
    # be fresh for every run. The host joins the graph, services and execution.
    host = QuantConnectHost(
        algorithm,
        deployment.graph_factory(),
        tickers=deployment.tickers,
        constraints=deployment.constraints,
        policy=deployment.execution,
        cost_rate=env.slippage,
        leverage=env.leverage,
    )
    # Startup diagnostics record settings and versions. Actual source identity
    # is retained separately by the cloud packaging/submission tools.
    emit(
        algorithm,
        "SUGARLOAF_RUN",
        {
            "name": deployment.name,
            "strategy": deployment.strategy,
            "environment": env,
            "execution": deployment.execution,
            "versions": {
                "sugarloaf": __version__,
                "python": platform.python_version(),
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
            },
        },
    )
    return host


def finish(algorithm, deployment, host):
    """Emit SUGARLOAF_RESULT diagnostics and the optional strategy report at run end."""
    def record(label, value):
        # Keep study records separate: QC limits individual log entry length.
        algorithm.log(label + " " + json.dumps(serialize(value), default=str, sort_keys=True))

    # Give the strategy graph access and a neutral record sink, not the QC host.
    # The callback can inspect its nodes without choosing a platform log API.
    context = ReportContext(host.runtime, record)
    # QC cuts a log entry at about 8,000 characters, and a universe's member list
    # alone can exceed that. The summary carries each universe's size; the members
    # go out as separate records, a hundred to a record.
    memberships = {}
    for name, membership in host.services.memberships.items():
        members = [instrument.value for instrument in membership.members]
        memberships[name] = {"cutoff": membership.cutoff, "ready": membership.ready,
                             "count": len(members)}
        for first in range(0, len(members), 100):
            record("SUGARLOAF_MEMBERS", {"universe": name, "first": first,
                                         "members": members[first:first + 100]})
    # The optional callback emits individual records while this dictionary is
    # built; its return value becomes the strategy summary in the final record.
    # Diagnostic tails keep that generic summary bounded.
    emit(
        algorithm,
        "SUGARLOAF_RESULT",
        {
            "batches": host.sequence,
            "runtime_diagnostic_count": host.runtime.diagnostic_count,
            "execution_diagnostic_count": host.execution.diagnostic_count,
            "runtime_diagnostics": list(host.runtime.diagnostics)[-20:],
            "execution_diagnostics": list(host.execution.diagnostics)[-20:],
            "memberships": memberships,
            "strategy": deployment.report(context) if deployment.report else {},
            "note": "Orders, fills and performance statistics are in the platform report.",
        },
    )
