# Maintenance

Start from the behaviour to change, its owning module and its tests. [Architecture](ARCHITECTURE.md)
gives the call chain; the component references and the [API pages](api/index.md) give the
contracts.

## Choose the boundary

| Change | Reference | Tests |
| --- | --- | --- |
| Declarations, records, slices | [Core](technical/CORE.md) | `tests/test_runner.py`, `tests/test_memory_adapter.py` |
| Views, graph wiring | [Core](technical/CORE.md) | `tests/test_view_axes.py`, `tests/test_runner.py` |
| Dispatch, view carriage, warm-up, terminal check | [Runtime](technical/RUNTIME.md) | `tests/test_runner.py` |
| QuantConnect mapping | [Adapters](technical/ADAPTERS.md) | No local tests; the backtests in [validation](VALIDATION.md#native-checks), run with `tools/native_checks.py` |
| Memory adapter | [Adapters](technical/ADAPTERS.md#memory) | `tests/test_memory_adapter.py` |
| Built-in nodes, allocation, constraints | [Portfolio](technical/PORTFOLIO.md) | `tests/test_nodes.py` |
| Order sizing and policy | [Execution](technical/EXECUTION.md) | `tests/test_memory_adapter.py`; on QuantConnect, the native checks |
| Archive, outcomes, metrics | [Research](technical/RESEARCH.md) | `tests/test_research_evaluation.py`, `tests/test_research_inference.py` |
| Examples and public deployments | [Examples](../examples/README.md) | `tests/test_examples.py` |
| Packaging, cloud tooling, evidence | [Deployment](technical/DEPLOYMENT.md) | `tests/test_deployment_bundle.py`, `tests/test_deployment_evidence.py`, `tests/test_cloud_run.py`, `tests/test_qc_client.py`, `tests/test_qc_kernels.py`, `tests/test_cli.py` |
| Native-check script | [Validation](VALIDATION.md#native-checks) | `tests/test_native_checks.py` |

Write a regression that tells the intended behaviour from the failure: a numerical case for
arithmetic, a sequence of `deliver`, `fire` and `set_members` calls on `MemoryAdapter` for
dispatch. A change to the QuantConnect adapter is verified by a native backtest, not a local
test.

## Verify a change

From the repository root:

```powershell
uv run --cache-dir .uv-cache pytest
uv run --cache-dir .uv-cache ruff check .
```

The QuantConnect adapter has local tests only for its bar-order rules
(`tests/test_qc_window_order.py`); its LEAN calls are verified by the backtests listed in
[validation](VALIDATION.md#native-checks), run with `tools/native_checks.py`. After a change
to the adapter, the launcher or a LEAN call, run the native checks:

```powershell
uv run --cache-dir .uv-cache python tools/native_checks.py deployments/quantconnect/native_checks.json --dry-run
uv run --cache-dir .uv-cache python tools/native_checks.py deployments/quantconnect/native_checks.json
```

The first command lists what would run without contacting QuantConnect. The second runs
each listed manifest as a QuantConnect backtest, one after another, compares `Total Orders`
exactly and the monetary and percentage statistics within each check's tolerance, prints one
line per check and writes the backtest ids and statistics to
`artifacts/native_checks/<UTC time>.json`. It exits nonzero when a check fails or a run
raises. `tests/test_native_checks.py` tests the script's comparison and file handling with a
`run_backtest` replacement that returns the statistics each test lists. A failing check either reveals a regression or reflects an intended
change; for an intended change, record the new backtest in [validation](VALIDATION.md) with
its project, backtest id and source hash, and update the expectations in
`deployments/quantconnect/native_checks.json`. A new LEAN call gets a check that exercises
it.

## Diagnose missing output

Check in order:

1. Did the adapter deliver the record? On QuantConnect, a feed with an `interval` delivers a
   bar only when its consolidator completes one; a bar's resolution must match the feed
   (`MemoryAdapter.deliver` ignores bars of another duration).
2. Did the node's selection include the instrument at that moment (`ctx.members` for a
   universe)?
3. Did the node run? It runs when its declared part of the slice is not empty, when a
   producer's outputs changed in this callback, or when its schedule fired.
4. Did it raise `NotReady`? Read `runner.diagnostics` (the last 2,000) and
   `runner.diagnostic_count`; on QuantConnect the last 20 are in `result.json` under `run`.
5. Did a consumer's port have fewer views than its `min_count`? That is a `NotReady`
   diagnostic recorded against the consumer; views of the wrong class or too many views raise
   `ContractError`. Expired views are removed before the count.
6. Was the run warming up? Outputs during warm-up are discarded.
7. In a trading run, did the terminal port hold exactly one `PositionView` that differed from
   `runner.last_target`, had not expired and passed the constraints (`TARGET_REJECTED`)?

`ContractError` and `CapabilityError` propagate and stop the run; they mean invalid wiring,
units, identities or an unsupported request.

## Diagnose orders

`runner.orders` holds one `OrderRecord` per instrument the adapter handled (`submitted`,
`filled`, `skipped` with reason `no mark`, `rejected` with the platform's reason);
`runner.updates` holds the platform's `OrderUpdate`s with cumulative fills. Orders are tagged
with the target's `view_id`, so a LEAN order list joins to targets by tag. An instrument with
no order usually means `order_quantity` returned zero: check `min_trade_value`,
`rebalance_band` and pending open orders against the [sizing rule](technical/EXECUTION.md#sizing).

For a QuantConnect run, read `SUGARLOAF_RUN`, `SUGARLOAF_RESULT`, the backtest's errors and
`result.json`, and keep the source identity tied to the run.

## Maintain documentation

- Docstrings (Google style: Args, Returns, Raises, Attributes) describe one object's
  contract; the API pages render them.
- Technical references describe interactions, timing and assumptions; READMEs introduce a
  package and link to the references.
- Each contract is written once and linked from elsewhere.
- [Validation](VALIDATION.md) changes only with new evidence.

Build the site with the documentation dependency group:

```powershell
uv sync --group docs --cache-dir .uv-cache
uv run --group docs --cache-dir .uv-cache mkdocs build --strict
```

`tools/docs.py` publishes the repository's pages and selected source files at their
repository paths, so links work on GitHub and in the site. The build reads source
statically and imports no LEAN code. Mermaid diagrams load a JavaScript library from a CDN.
After a documentation change, check links and anchors, run every snippet the docs present as
runnable, and confirm that every name the docs mention exists in the source.

## Operational limits

There is no restart or recovery of runner state: node state, current views, the archive and
the last target live in memory for one run. The forecast archive is unbounded within a run;
diagnostics, order records, updates and the view-identity memory are bounded. Credentials
and generated data stay outside source control; ignored artifacts need their own backup.
