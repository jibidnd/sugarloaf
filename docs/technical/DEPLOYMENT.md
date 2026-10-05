# Deployment and retained evidence

An explicit manifest selects a strategy for a QC run. The public run interface
uploads, compiles, backtests and retains submitted source and retrieved results.
Shared configuration, evidence and reporting live in `sugarloaf.deployment`.
The shared `sugarloaf.cli` entrypoint selects `--adapter` and forwards all other
arguments unchanged. It does not open a manifest or define provider run options.
QC packaging, cloud operations and remote notebooks live
in `sugarloaf.adapters.quantconnect.cloud`, separate from the adapter running
inside LEAN. QC hosts callbacks, native data, clocks, indicators and order APIs;
Sugarloaf owns graph decisions, trading sessions and execution reconciliation.

Source: [configuration](../../sugarloaf/deployment/config.py),
[adapter dispatch](../../sugarloaf/cli.py),
[bundles](../../sugarloaf/adapters/quantconnect/cloud/packaging.py),
[cloud CLI](../../sugarloaf/adapters/quantconnect/cloud/backtests.py),
[evidence](../../sugarloaf/deployment/evidence.py),
[reporting](../../sugarloaf/deployment/reporting.py) and
[remote research](../../sugarloaf/adapters/quantconnect/cloud/notebooks.py).

## Deployment and Environment

A manifest's `module:function` factory receives a parameter lookup callable and
returns `Deployment`. See the [public factory](../../deployments/quantconnect/config.py).
The factory chooses which parameter names it consumes; the manifest and command
line pass strings rather than configuring arbitrary node fields automatically.

| Field | Responsibility |
| --- | --- |
| `name` | Run identity in diagnostic reports |
| `strategy` | Caller-supplied reportable settings |
| `graph_factory` | Zero-argument factory building fresh compiled node state |
| `tickers` | Initial stable-ID to platform-ticker bindings |
| `environment` | Dates, currency, capital, timezone, benchmark, brokerage, leverage, slippage |
| `execution` | Order policy, intent lifetime, target age and trade thresholds |
| `constraints` | Execution safety limits |
| `report` | Optional end-of-run callback receiving `ReportContext(runtime, record)`; returns summary diagnostics |

`Environment` dates are inclusive platform dates in its timezone. Cash is in
account currency; slippage is a proportional notional rate. Brokerage/account type
names resolve to native enum members. The launcher applies native settings, builds
the graph/host, emits `SUGARLOAF_RUN` at start and `SUGARLOAF_RESULT` at end.

Node declarations determine subscriptions and schedules. Graph constraints are
configured in the graph factory; deployment constraints independently protect
execution. `main.py` forwards callbacks rather than containing strategy logic.

Factories select fixed instruments or updating universes through node requirements.
Factory parameters affect selection only when the factory explicitly reads them.
`Deployment.tickers` supplies initial symbol bindings; investable membership comes
from the node's declared `Selection` and any `UniverseRequirement`. Native
membership handling stays in the adapter.

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

QC Cloud rejects `types.py`; bundling maps `core/types.py` to `core/primitives.py`
and rewrites Python/notebook imports. Local imports remain unchanged.
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

These actions require an installed, authenticated LEAN client and the chosen QC
account/project. LEAN is separate tooling rather than a library runtime dependency.
Its stored authentication supplies the API client; credentials never enter bundles.

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
must be finite and positive. These limits bound polling between requests. LEAN
owns network requests, authentication and retries; a request can outlast a polling
limit, and a timeout does not cancel a remote job. Inspect the retained IDs before
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
The inspected LEAN 1.0.180 client owns authentication and HTTP handling, retries
an HTTP 5xx response once, and supplies no default network-request timeout on
this path (`APIClient._request` and `HTTPClient.request`). Sugarloaf adds no
network retry policy; its waiting limits apply between those client calls.

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
notebook on a newly created remote Jupyter kernel and retains outputs. It requires
the authenticated LEAN environment plus its notebook/network dependencies.
The tool uses QC extension endpoints, not a documented API stability promise.
Connection tokens stay in memory; only its created kernel is shut down.

This runner is distinct from `sugarloaf.research.run_node`: one operates a remote
notebook; the other performs predictive-node replay inside a supplied provider.
See [research](RESEARCH.md) for the latter.

## Verification

[Bundle tests](../../tests/test_deployment_bundle.py) cover explicit inclusion and
import rewriting; [evidence tests](../../tests/test_deployment_evidence.py) cover
snapshot checks, historical lookup, uncertain submissions and log pagination.
[Validation](../VALIDATION.md) records completed platform checks and remaining limits.
The public run orchestration and cloud-tool boundaries have local regression
coverage. QC cloud evidence applies only to its retained source snapshots;
current orchestration has not received cloud acceptance validation.
