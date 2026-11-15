# Deployment and retained evidence

An explicit TOML manifest selects a strategy for a QuantConnect run. The cloud tooling
uploads, compiles, backtests and retains the submitted source and the retrieved results.
Shared configuration, evidence and reporting live in `sugarloaf.deployment`. The shared
`sugarloaf.cli` entry point selects `--adapter` and forwards every other argument unchanged.
QuantConnect packaging, cloud operations and remote notebooks live in
`sugarloaf.adapters.quantconnect.cloud`, separate from the adapter that runs inside LEAN.

Source: [configuration](../../sugarloaf/deployment/config.py),
[adapter dispatch](../../sugarloaf/cli.py),
[launcher](../../sugarloaf/adapters/quantconnect/launcher.py),
[bundles](../../sugarloaf/adapters/quantconnect/cloud/packaging.py),
[cloud CLI](../../sugarloaf/adapters/quantconnect/cloud/backtests.py),
[evidence](../../sugarloaf/deployment/evidence.py),
[reporting](../../sugarloaf/deployment/reporting.py) and
[remote research](../../sugarloaf/adapters/quantconnect/cloud/notebooks.py).

## Deployment and Environment

A manifest's `module:function` factory receives a parameter lookup callable and returns a
`Deployment`. See the [public factories](../../deployments/quantconnect/config.py). The
factory chooses which parameter names it reads; the manifest and command line pass strings.

| Field | Meaning |
| --- | --- |
| `name` | Run identity in the result file |
| `strategy` | Caller-supplied settings, serialized into the result file |
| `graph_factory` | Zero-argument function returning a fresh `CompiledGraph`; called once per run |
| `tickers` | `{InstrumentId: ticker or Ticker}` for the instruments nodes name explicitly; universe members bind by security id |
| `environment` | `Environment`: dates, cash, currency, time zone, benchmark, brokerage, account type, slippage, leverage, borrow data |
| `execution` | `ExecutionPolicy`: order type, time in force, minimum trade value, rebalance band, cancellation of superseded orders |
| `constraints` | `PortfolioConstraints` the runner checks every terminal target against |
| `report` | Optional `report(context)` called at the end with `ReportContext(runner, record)`; its return value goes into the result file |

The graph's output port decides the run's mode: a port declaring exactly `PositionView` makes
a trading run; any other port makes a forecast run, which archives views and sends no orders.
[ema.toml](../../deployments/quantconnect/ema.toml) is a trading run;
[ema_forecasts.toml](../../deployments/quantconnect/ema_forecasts.toml) and
[index_check.toml](../../deployments/quantconnect/index_check.toml) are forecast runs.

`Environment` dates are inclusive platform dates in its time zone. Cash is in account
currency; slippage is a proportional rate applied through LEAN's `ConstantSlippageModel`.
`brokerage` and `account_type` name `BrokerageName` and `AccountType` members.
`borrow_data="interactive_brokers"` installs Interactive Brokers' shortable provider, which
also charges borrow fees on shorts.

Node declarations determine subscriptions, universes, indicators and schedules; the
deployment carries no data settings. Graph-level limits are nodes in the graph;
`Deployment.constraints` is checked by the runner on every terminal target.

## What a run does on QuantConnect

`SugarloafAlgorithm.initialize` ([main.py](../../sugarloaf/adapters/quantconnect/main.py))
calls `launcher.load`, which calls the factory with `algorithm.get_parameter` (and `stored` or
`fetch` when the factory asks for them), then `launcher.start`, which applies the
environment, builds `QuantConnectAdapter` and `GraphRunner`, calls `runner.start()` and logs
one line:

```text
SUGARLOAF_RUN {"mode": "trading", "name": "ema", "numpy": "...", "python": "...", "scipy": "...", "sugarloaf": "0.1.0"}
```

At the end `launcher.finish` saves to the Object Store under
`sugarloaf/results/<project_id>/<algorithm_id>/`:

| Key | Content |
| --- | --- |
| `result.json` | `name`, `mode`, `environment`, `execution`, `run` (`runner.finish()`), universe `members`, the last 5,000 `orders` and `updates`, the `strategy` report, `records` from `ReportContext.record`, each node's `config` |
| `forecasts.json` | Forecast runs only: `encode_archive(runner.archive)` |

and logs `SUGARLOAF_RESULT {"diagnostics": ..., "forecasts": ..., "key": ..., "orders": ..., "steps": ...}`.

## Manifest and bundle

[The EMA TOML](../../deployments/quantconnect/ema.toml) selects a root, factory,
include list and parameters. Exactly one factory or native entrypoint is required.
An optional project ID selects an existing cloud project.

`read_spec` resolves the manifest. `build_bundle` includes framework Python needed
inside LEAN and only selected strategy sources. Included directories contribute
Python files; notebooks require explicit file selection. Real package initializers
are preserved. Sources cannot escape the root or shadow generated infrastructure.
Local cloud tooling and the shared CLI are excluded from the uploaded
framework bundle. They execute in the caller's authenticated Python environment.

Files upload unmodified. QC Cloud rejects a module named `types.py`, so no
bundled source may use that basename.
SHA-256 manifests identify actual uploaded contents, including local edits;
a Git revision or package version alone is insufficient.

From the repository root:

```powershell
uv run --cache-dir .uv-cache sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

This is offline and writes evidence under `artifacts/ema/`.
Every QC action requires `--manifest PATH`, including the public example.
The shared command requires `--adapter quantconnect`; that adapter owns the TOML
schema and validation. Relative manifest paths resolve from the caller's current
directory; absolute paths may point directly into a separate Lab checkout.
[External strategies](../EXTERNAL_STRATEGIES.md) documents package layout and factories.

## Run a QC backtest

These actions use Sugarloaf's QC REST client
(`sugarloaf.adapters.quantconnect.cloud.client`), which authenticates as QC
documents (user ID and a SHA-256 hash of the API token with a timestamp). It reads
`QC_USER_ID` and `QC_API_TOKEN`, or the LEAN CLI's saved login
(`~/.lean/credentials`); the LEAN CLI itself is not needed. It uses only the
standard library. Credentials never enter bundles or records.

Select an existing project with `project_id` in the manifest or its saved
`cloud.json`. If neither selects a project, provision one explicitly once:

```powershell
sugarloaf create --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

Run the selected configuration:

```powershell
sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml --parameter ticker SPY --name "EMA check"
```

`run` uploads selected sources and effective parameters, requests compilation,
waits for `BuildSuccess`, submits a backtest, waits for completion with a nonempty
statistics dictionary, then retrieves logs. QC can mark a run completed before
publishing metrics; polling continues within the backtest wait limit until those
metrics arrive. Algorithm errors finish the wait immediately and are surfaced after
attempting log retrieval. It prints JSON with project, compile and backtest
IDs, source hash, parameters, statistics and the artifact path. A build or algorithm
failure raises an error; retained selection and evidence support inspection.

The same path is available to scripts:

<!-- snippet: fragment (needs QuantConnect credentials and a selected project) -->
```python
from sugarloaf.adapters.quantconnect.cloud.backtests import run_backtest

result = run_backtest(
    "deployments/quantconnect/ema.toml",
    parameters={"ticker": "SPY"},
    name="EMA check",
)
print(result["statistics"], result["artifacts"])
```

The defaults are a 120-second compile wait, an 1800-second backtest wait and a
5-second poll interval. Set `--compile-timeout`, `--backtest-timeout` and
`--poll-interval` on `run`, or their underscore-named Python arguments. Values
must be finite and positive. These limits bound polling between requests. Each
request has its own 120-second network timeout and one retry on an HTTP 5xx reply;
a timeout does not cancel a remote job. Inspect the retained IDs before
submitting again.

## Individual cloud actions

Use `sugarloaf ACTION --adapter quantconnect --manifest PATH` for an individual
step or inspection:

| Action | Effect |
| --- | --- |
| `package` | Build and retain the bundle offline |
| `create` | Provision a QC project and record its identity |
| `upload` | Upload selected sources/parameters; invalidate prior compile/run selection |
| `parameters` | Update parameters using repeated `--parameter NAME VALUE` |
| `compile`, `compile-status` | Request/read a build |
| `backtest` | Submit the selected compile; verify `BuildSuccess` with `compile-status` first |
| `status`, `logs` | Retrieve results and logs for the selected run |
| `snapshot --backtest-id ID` | Read a specific retained deployment offline |

Repeat `--parameter NAME VALUE` on `run`, `package`, `upload` or `parameters`.
Overrides merge with manifest defaults and are retained with the effective
configuration. `parameters` requires at least one override; a later upload/run
uses its own manifest defaults and supplied overrides. `--name` applies only to
`run` and `backtest`. `--backtest-id` applies to `status`, `logs` and `snapshot`,
and is mandatory for `snapshot`.

Submission includes the effective parameters explicitly in the backtest request,
as supported by QC's [Create Backtest API](https://www.quantconnect.com/docs/v2/cloud-platform/api-reference/backtest-management/create-backtest).
Uploads send files and parameters in one `projects/update` request with the form
encoding the LEAN CLI uses (`files[i][name]`, `parameters[i][key]`). The client
retries an HTTP 5xx reply once and adds no other retry policy.

For example, inspect a specific completed run using its saved identity:

```powershell
sugarloaf status --adapter quantconnect --manifest deployments/quantconnect/ema.toml --backtest-id RUN_ID
sugarloaf logs --adapter quantconnect --manifest deployments/quantconnect/ema.toml --backtest-id RUN_ID
sugarloaf snapshot --adapter quantconnect --manifest deployments/quantconnect/ema.toml --backtest-id RUN_ID
```

`backtest` submits the selected compile without waiting; check `compile-status`
for `BuildSuccess` first. It uses uploaded/saved parameters. Project creation and
backtests are outward actions with provider costs/limits. No command above runs
automatically as part of local setup.

Upload clears compilation/submission eligibility before sending sources. If the
response is lost, upload again successfully before compiling; a later build must
not be attributed to the previous bundle. Explicit historical run lookup remains
available through its retained snapshots.

A successful compile proves build/import checks. Inspect the completed run's
errors, diagnostic logs and relevant outputs before claiming runtime behavior.
Neither a build nor a completed example establishes trading edge.

## Evidence layout and historical lookup

Under `artifacts/<manifest-stem>/`:

| Path | Meaning |
| --- | --- |
| `cloud.json` | Current project/compile/run selection |
| `bundle.json` | Convenience copy of the current bundle |
| `sources/`, `bundles/` | Content-addressed actual source and configuration snapshots |
| `compilations/` | Immutable compile-status observations with deployment identity |
| `<backtest-id>-deployment.json` | Frozen submitted-run identity |
| `<backtest-id>.json`, `<backtest-id>-logs.json` | Convenience retrieved result/log copies |
| `observations/<backtest-id>/` | Immutable retrieved observations |
| `submissions/`, `submission-outcomes/` | Before-request identity and accepted/uncertain outcome |

`persist_bundle` retains immutable source/bundle snapshots. `run_snapshot` loads
a named run and checks its referenced snapshot hashes. Explicit `status`/`logs`
with `--backtest-id ID` use that run's saved project/source identity, not current
selection. Missing requested evidence fails instead of silently substituting a run.
Older evidence without actual retained sources has correspondingly limited provenance.

Submission intent is saved before the API call. An uncertain response leaves
evidence of the unresolved request; resolve it before retrying to avoid duplicate
paid runs. This evidence mechanism does not create broker/runtime restart recovery.

Log retrieval uses QC's
[exclusive-end 200-line pages](https://www.quantconnect.com/docs/v2/cloud-platform/api-reference/backtest-management/read-backtest/logs)
until all reported lines are collected. Incomplete pages or changing log length
fail visibly; retry after completion. Provider retention/quotas still bound available
logs. The tool does not export every order/chart or synchronize in the background.
Ignored local artifacts need private backup and tested restoration.

## Remote notebook tool

`python -m sugarloaf.adapters.quantconnect.cloud.notebooks NOTEBOOK --project-id ID` runs a local
notebook on a newly created remote Jupyter kernel and retains outputs. It needs
QC credentials (above), `nbformat` and `websocket-client`. The research connection
uses `research/read` and `research/create`, which QC says are not part of its
official API. Connection tokens stay in memory; only its created kernel is shut down.

`sugarloaf.adapters.quantconnect.cloud.kernels` exposes the pieces for persistent
kernels: `connect`, `start_kernel`, `run_cell` (one cell; on timeout the kernel is
interrupted, not deleted), `kernel_status`, `stop_kernel`, and `ResearchKernel`,
which tracks idle time against an `idle_timeout` so its owner can stop it.

A notebook that scores a forecast run reads the run's `forecasts.json` from the Object Store
of the same project; see [research](RESEARCH.md).

## Verification

[Bundle tests](../../tests/test_deployment_bundle.py) cover explicit inclusion,
path containment and source shadowing; [example tests](../../tests/test_examples.py) build the public deployments;
[evidence tests](../../tests/test_deployment_evidence.py) cover
snapshot checks, historical lookup, uncertain submissions and log pagination, with a REST
client whose `post` returns the responses each test lists. The QuantConnect adapter and the
launcher have no local tests; they are verified by the backtests listed in
[validation](../VALIDATION.md#native-checks), run with `tools/native_checks.py`.
[Validation](../VALIDATION.md) records completed platform checks and remaining limits.
The public run orchestration and cloud-tool boundaries have local regression
coverage. QC cloud evidence applies only to its retained source snapshots;
current orchestration has not received cloud acceptance validation.
