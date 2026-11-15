# Sugarloaf Guided Tutorial Syllabus

This is the source map for a guided explanation, not a reading list or an assignment sheet.
All implementation and test paths below are relative to the Sugarloaf repository root.
Teach one numbered subsection per turn and preserve the learner's requested pace. Read only
the current topic's sources.

There are 12 sections and 36 subsections. Use these titles and numbers in the progress
checklist. Teach standalone usage from Sugarloaf's own checkout and public examples. An
optional separate strategy repository can organize user code; it is not a prerequisite.
Walk through usable commands and snippets yourself without turning them into assignments.

## 1 Repository orientation and infrastructure

- **1.1 What Sugarloaf owns:** nodes, Views, graphs, `GraphRunner`, adapters, execution
  sizing, research scoring and deployment. The three parts (node, runner, adapter) and the
  dependency boundary for an external strategy package; LEAN's role as the platform.
- **1.2 Finding your way around the repository:** the `sugarloaf/` package map;
  `examples/`, `tests/`, `docs/`, `deployments/` and ignored `artifacts/`.
- **1.3 Python environments and local tooling:** Python version and dependencies from
  `pyproject.toml`; uv and the lockfile; test, lint and docs commands; CI. Source identity
  versus package version.

Sources: [README](../../../../README.md), [guide](../../../../docs/GUIDE.md),
[architecture](../../../../docs/ARCHITECTURE.md), `pyproject.toml`, `uv.lock`,
`.github/workflows/checks.yml`, `deployments/README.md`.

## 2 One complete strategy

- **2.1 EMA from bars to positions:** `EMAConfig`, `EMAScore` (one `Bars` input, two
  `Indicator` declarations, `update`), `LongWhenPositive.update`. Define NAV and an
  uncalibrated score.
- **2.2 The trading graph and the forecast graph:** `build_graph` and
  `build_forecast_graph`; the output port decides trading versus forecast run. Distinguish a
  signal, a target, an order and a fill.
- **2.3 The memory example:** `FixedContribution` with a `Schedule`, `PositionNetter`,
  `PortfolioConstraintNode`, `MemoryAdapter.fire`; why the run reports one diagnostic. Show
  `uv run --cache-dir .uv-cache python -m examples.memory`.

Sources: [examples](../../../../examples/README.md), `examples/ema.py`,
`examples/memory.py`, `tests/test_examples.py`.

## 3 Views and core contracts

- **3.1 View families and PositionView:** `View(target, statement, metadata)`, the named
  subclasses (`ReturnView`, `RelativeReturnView`, `RankingView`, `ProbabilityView`,
  `DistributionView`, `ScoreView`) and `PositionView`.
- **3.2 Targets, units, horizons and conventions:** `InstrumentId`, `Target`, `Interval`,
  `return_target`, `relative_return`, decimal returns, weights versus quantities,
  price versus total return.
- **3.3 Metadata, identity, expiry and lineage:** `Metadata` fields; one identity, one
  content; expiry and withdrawal are not liquidation.

Sources: [core](../../../../docs/technical/CORE.md#views), `sugarloaf/core/primitives.py`,
`sugarloaf/core/views.py`, `tests/test_view_axes.py`.

## 4 Nodes and graph compilation

- **4.1 The node protocol:** declarations (`inputs`, `indicators`, `schedules`,
  `warm_up`), `ports`, `outputs`, `config`; callbacks `update`, `on_scheduled`,
  `on_securities_changed`; `NODE_DEFAULTS`. Walk through the guide's minimal node.
- **4.2 Graph construction and compilation:** `Graph.add_node`, `connect`, `set_output`,
  `compile`; `InputSpec`, `OutputSpec`, `NodeInputs`; `CompiledGraph.emits_positions`.
- **4.3 Node state and output replacement:** a node owns its state; a callback returns its
  complete output; `None` withdraws; restating the previous output keeps it; fresh nodes per
  run through `graph_factory`.

Sources: `sugarloaf/core/node.py`, `sugarloaf/core/graph.py`, `examples/ema.py`,
[guide section 2](../../../../docs/GUIDE.md#2-write-a-node), `tests/test_runner.py`.

## 5 Declarations and the slice

- **5.1 Data declarations:** `Selection`, `Bars`, `Quotes`, `Custom`, `Bars.key` sharing,
  `interval` consolidation, `window`, `normalization`.
- **5.2 Indicators, schedules, warm-up, history:** `Indicator`, `Schedule`, `WarmUp`,
  `HistoryRequest`; what the platform does with each.
- **5.3 Universes and the slice:** `LiquidityUniverse`, `ETFConstituentsUniverse`,
  `FundamentalUniverse`, `select`; `Slice` members, `series(name)`, lifecycle records;
  `ctx.window`, `ctx.indicator`, `ctx.members`, `ctx.calendar`, `ctx.portfolio`.

Sources: [core declarations](../../../../docs/technical/CORE.md#declarations),
`sugarloaf/core/inputs.py`, `sugarloaf/core/data.py`, `sugarloaf/core/universes.py`.

## 6 The runner

- **6.1 Start-up:** `GraphRunner.__init__` gathering declarations, `start()` and the order of
  adapter setup calls; shared feeds; the longest warm-up.
- **6.2 Dispatch:** `on_data`, `on_scheduled`, `on_securities_changed`, `on_order_event`;
  the per-node slice; topological order; which nodes run; view carriage and expiry.
- **6.3 Checks and failures:** output validation (`_check`), `NotReady` and `NumericalError`
  as diagnostics, propagated `ContractError`, warm-up discarding outputs, the terminal target
  and `TARGET_REJECTED`.

Sources: [runtime](../../../../docs/technical/RUNTIME.md), `sugarloaf/runtime/runner.py`,
`tests/test_runner.py`.

## 7 Adapters

- **7.1 The adapter interface:** `Adapter`, `Arrival`, `IndicatorReading`, `Calendar` in
  `sugarloaf/adapters/protocol.py`; setup, read and action methods.
- **7.2 QuantConnect mapping:** `QuantConnectAdapter` construction, `subscribe` and
  consolidators, universes and the selector, custom data, indicator helpers, schedules,
  warm-up, `execute`, `on_data`, `on_securities_changed`, `on_order_event`; time zones; the
  mapping table.
- **7.3 Memory adapter and the launcher:** `MemoryAdapter` driving methods and the calendars,
  histories and lot sizes the caller supplies;
  `SugarloafAlgorithm`, `launcher.load`, `launcher.start`, `launcher.finish`.

Sources: [adapters](../../../../docs/technical/ADAPTERS.md),
`sugarloaf/adapters/quantconnect/adapter.py`, `launcher.py`, `main.py`,
`sugarloaf/adapters/memory/adapter.py`, `tests/test_memory_adapter.py`; the QuantConnect
adapter has no local tests and is verified by the backtests listed in
[validation](../../../../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.

## 8 Portfolio composition

- **8.1 Indicator scores, aggregation and allocation:** `EMASpread`, `RSIReversion`,
  `SMATrend`, `horizon_end`, `MeanViewAggregator`, `PositiveSignalPositions`.
- **8.2 Branch budgets, netting and attribution:** `PositionNetter`; walk contributions of
  20% and 5% in one asset.
- **8.3 Constraints and explicit exits:** `PortfolioConstraintNode`,
  `PortfolioConstraints`, `violations`, `breaches`; the runner's own check against
  `Deployment.constraints`.

Sources: [built-in nodes](../../../../sugarloaf/nodes/README.md),
[portfolio](../../../../docs/technical/PORTFOLIO.md), `sugarloaf/nodes/technical.py`,
`simple.py`, `positions.py`, `sugarloaf/portfolio/constraints.py`, `tests/test_nodes.py`.

## 9 Orders and execution

- **9.1 From target to orders:** the terminal check, `adapter.execute(target, policy)`,
  `OrderRecord`, tags.
- **9.2 Sizing:** `ExecutionPolicy`, `desired_quantity`, `order_quantity`, the rebalance
  band and `min_trade_value`; walk the 19.92-share example.
- **9.3 Platform behaviour and feedback:** market, market-on-open and market-on-close
  orders in LEAN; cancellation of superseded orders; `OrderUpdate` and cumulative fills;
  the memory adapter's fills.

Sources: [execution](../../../../docs/technical/EXECUTION.md),
`sugarloaf/execution/contracts.py`, `tests/test_memory_adapter.py`; on QuantConnect, the
native checks in [validation](../../../../docs/VALIDATION.md#native-checks).

## 10 Predictive research and methodology

- **10.1 Forecast runs:** a graph ending in a predictive port; `ForecastArchive.record`;
  `encode_archive` and `decode_archive`; view metrics computed in the run and written to the
  backtest log (`VIEW_METRICS`, `sugarloaf.research.view_metrics`); research on
  prices with `QuantBook` directly.
- **10.2 Outcome resolution:** `anchor_from_closes`, `closes_from_frame`,
  `ReturnOutcomeResolver`, exact endpoints, `as_of`, pending, unavailable and unsupported
  labels. Walk the guide's local forecast example and the EMA notebook.
- **10.3 Metrics, coverage and evaluation discipline:** `evaluate`,
  `EvaluationReport.coverage` and `summary`, cohorts, `block_bootstrap_interval`,
  `cross_sectional_ic`; overlapping horizons; signal quality versus P&L.

Sources: [research](../../../../docs/technical/RESEARCH.md), `sugarloaf/research/`,
`examples/ema_research.ipynb`, `examples/EMA_RESEARCH.md`,
`tests/test_research_evaluation.py`, `tests/test_research_inference.py`.

## 11 Deployment and evidence tooling

- **11.1 Deployment factories and manifests:** `Deployment`, `Environment`,
  `graph_factory`, ticker bindings and `Ticker`; `module:function` factories; `include`;
  the three public manifests.
- **11.2 Packaging and cloud operations:** `read_spec`, `build_bundle`, offline package
  inspection, project selection, upload, compile, backtest, status and logs; the remote
  notebook runner.
- **11.3 Run identity and results:** source hashes, snapshots, compile and backtest ids,
  `SUGARLOAF_RUN`, `RUN_SUMMARY`, `VIEW_METRICS`, `VIEW_SAMPLE`, `read_run_output`; the
  Object Store for fitted models and reusable datasets only.

Sources: [external factories](../../../../docs/EXTERNAL_STRATEGIES.md),
[deployment](../../../../docs/technical/DEPLOYMENT.md), `deployments/quantconnect/`,
`sugarloaf/deployment/`, `sugarloaf/adapters/quantconnect/cloud/`,
`tests/test_deployment_bundle.py`, `tests/test_deployment_evidence.py`.

## 12 Maintenance and debugging

- **12.1 Tracing diagnostics:** `runner.diagnostics`, `NotReady` versus `CapabilityError`
  versus `ContractError`; the missing-output checklist.
- **12.2 Local checks, CI and native validation:** pytest, Ruff, the docs build; the
  QuantConnect adapter verified only by native backtests, run with `tools/native_checks.py`;
  compile success versus a completed backtest versus economic validity; the calls not yet
  exercised natively.
- **12.3 Where to change what and operational limits:** signal, allocation, order policy,
  constraints, adapter mapping; no restart or recovery; retention limits. Preserve
  dependency, commit/push, compute, release/live/capital and external-action gates.

Sources: [maintenance](../../../../docs/MAINTENANCE.md),
[validation](../../../../docs/VALIDATION.md), `sugarloaf/core/errors.py`, `tests/`,
`.github/workflows/checks.yml`, `AGENTS.md`.

## Essential traces to connect across sections

Teach these progressively when their components appear, not all on the first turn:

```text
initialize -> launcher.load -> launcher.start -> QuantConnectAdapter + GraphRunner
-> runner.start -> subscribe / add_universe / add_custom / add_indicator / schedule / warm_up

LEAN on_data -> QuantConnectAdapter.on_data -> Arrival -> GraphRunner.on_data
-> per-node Slice -> update in topological order -> views along edges
-> changed terminal PositionView -> constraints -> adapter.execute -> market_order

scheduled function -> runner.on_scheduled(name) -> on_scheduled -> dependents
LEAN on_securities_changed -> adapter windows and indicators -> runner.on_securities_changed
LEAN on_order_event -> OrderUpdate -> runner.updates

forecast graph -> runner.archive -> launcher.finish -> adapter.history -> view_metrics
-> VIEW_METRICS log line -> read_run_output (REST API) -> run output on disk
```

Show the current implementation's actual branches, ordering and error paths when explaining
a concrete callback.
