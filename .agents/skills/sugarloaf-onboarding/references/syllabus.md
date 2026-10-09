# Sugarloaf Guided Tutorial Syllabus

This is the source map for a guided explanation, not a reading list or an
assignment sheet. All implementation and test paths below are relative to the
Sugarloaf repository root. Teach one numbered subsection per turn and preserve
the learner's requested pace. Read only the current topic's sources.

There are 12 sections and 36 subsections. Use these titles and numbers in the
progress checklist. A complete tutorial connects architecture, actual code,
research methodology, operational tools, and the evidence needed for maintenance.
Teach standalone usage from Sugarloaf's own checkout and public examples. An
optional separate strategy repository can organize user code; it is not a
prerequisite. Walk through usable commands and snippets yourself without turning
them into assignments.

## 1 Repository orientation and infrastructure

- **1.1 What Sugarloaf owns:** reusable framework responsibilities; nodes, Views,
  graphs, runtime, adapters, execution, research, and deployment. Explain the
  dependency boundary for an external strategy package and the role of LEAN.
- **1.2 Finding your way around the repository:** `sugarloaf/` component map;
  `examples/`, `tests/`, `docs/`, `deployments/`, and ignored `artifacts/`. Separate
  installable deployment utilities from compatibility wrappers and examples.
- **1.3 Python environments and local tooling:** Python version and dependencies
  from `pyproject.toml`; uv and lockfile; test/lint commands; Git and CI; LEAN as
  separate deployment tooling. Show the checkout's setup commands and how to
  make the framework available in a strategy environment without assuming a
  published package release. Explain source identity versus package version.

Sources: [README](../../../../README.md),
[guide](../../../../docs/GUIDE.md), `pyproject.toml`, `uv.lock`,
`.github/workflows/checks.yml`, and `deployments/README.md`.

## 2 One complete strategy

- **2.1 EMA from observations to positions:** `EMAConfig`, `EMAScore.evaluate`,
  and `LongWhenPositive.evaluate`; native fast/slow EMA spread becomes a ScoreView,
  then an allocation rule emits a PositionView. Define NAV and uncalibrated score.
- **2.2 The trading graph and execution path:** follow `build_graph`, the configured
  final output, TradingSession, and execution at a high level. Distinguish a signal,
  target, order intent, broker order, and actual fill.
- **2.3 What memory examples demonstrate:** walk `FixedContribution` branches,
  netting, constraints, supplied partial/full fills, and replay. Explain the native
  versus batch-reference indicator examples and their different purposes. Show
  `uv run --cache-dir .uv-cache python -m examples.memory` as the local entry point,
  describe its expected behavior, and distinguish local fixtures from QC services.

Sources: [examples](../../../../examples/README.md),
`examples/ema.py`, `examples/memory.py`, `examples/technical/README.md`,
`tests/test_technical_examples.py`.

## 3 Views and core contracts

- **3.1 Predictive View families and PositionView:** ReturnView,
  RelativeReturnView, RankingView, ProbabilityView, DistributionView, ScoreView,
  and desired positions. Explain which claims each can represent.
- **3.2 Targets, units, horizons, and conventions:** InstrumentId, Target,
  Interval, return differences, scalar/joint targets, decimal returns,
  account-NAV weights versus quantities, calendar horizons, and price/total return.
- **3.3 Metadata, identity, expiry, and lineage:** cutoff versus target horizon,
  expires_at, source, parent IDs, and model version. Reusing an identity with
  different contents is invalid; expiry and withdrawal are not liquidation.

Sources: [core contracts](../../../../docs/technical/CORE.md),
`sugarloaf/core/primitives.py`, `views.py`, `events.py`, and `tests/test_contracts.py`.
Here and below, adjacent bare filenames share the preceding component directory.

## 4 Nodes and graph compilation

- **4.1 Node protocol, ports, requirements, and triggers:** structural Node
  protocol; InputSpec/OutputSpec; NodeInputs/NodeOutputs; evaluate(inputs, ctx).
  Explain the division between declared resources and activation rules. Walk
  through a minimal node using public imports so the learner can author their own.
- **4.2 Graph construction and compilation:** Graph.add_node/connect/set_output/
  compile; CompiledGraph; dependency order, port compatibility, cycle rejection,
  and why a trading graph must designate a position output. Show a complete small
  graph assembled from existing nodes and where custom nodes would connect.
- **4.3 Fresh state and complete output replacement:** run-specific node state,
  omissions withdrawing old ports, immutable output snapshots, runtime checks,
  and resource updates becoming effective on the next batch.

Sources: `sugarloaf/core/node.py`, `sugarloaf/core/graph.py`,
`examples/ema.py`, `tests/test_contracts.py`, `tests/test_runtime.py`.

## 5 Events, clocks, and runtime

- **5.1 EventBatch and the different clocks:** Event, EventBatch, sequence,
  observation end, receipt/availability, and normalized graph cutoff. Walk a bar
  ending 10:01 received at 10:03, then an older bar arriving later.
- **5.2 GraphRuntime.process and selective activation:** install pending resource
  declarations, provider.apply, expire Views, find dirty nodes, readiness,
  topological evaluation, output validation/replacement, and consumer activation.
  Explain schedules crossed on arriving data and coalesced missed occurrences.
- **5.3 Replay, expiry, failure, and lifecycle invalidation:** identical replay,
  conflicting/regressing sequences, equal-time arrivals, ViewStore.replace/expire/
  withdraw, NotReady/NumericalError diagnostics, propagated errors, and conservative
  lifecycle invalidation. No independent node timer or graph-clock rewind.

Sources: `sugarloaf/runtime/runtime.py`, `sugarloaf/core/events.py`,
`tests/test_data_clock.py`, `tests/test_runtime.py`.

## 6 Context and data resources

- **6.1 Lazy services and causal reads:** Context.market/portfolio/instruments/
  costs/valuation/constraints; admitted information, snapshot lifetime and version
  invalidation, declared reads, exact anchors, and snapshot-local ctx.compute.
- **6.2 Readiness, freshness, windows, and indicators:** BarsRequirement,
  QuotesRequirement, IndicatorRequirement, and account/instrument
  requirements. Distinguish source lookback, indicator period, output window,
  normalization, sessions, fill-forward, and max_age.
- **6.3 Resource ownership, universes, and deferred updates:**
  ProviderServices.install/apply/context/ready; shared subscriptions and retention,
  Selection and point-in-time membership, liquidity/ETF universes, pending
  ResourceUpdate changes, cleanup, and membership versus execution ownership.

Sources: [adapters](../../../../docs/technical/ADAPTERS.md),
`sugarloaf/core/services.py`, `sugarloaf/market/contracts.py`,
`sugarloaf/adapters/base.py`, `sugarloaf/adapters/memory/services.py`,
`tests/test_adapter_memory.py`.

## 7 QuantConnect integration

- **7.1 Startup and deployment composition:** SugarloafAlgorithm.initialize,
  deployment loading, launcher.start, QuantConnectHost, QuantConnectServices,
  QuantConnectGateway, ExecutionEngine, TradingSession, and GraphRuntime.
- **7.2 Callbacks, consolidation, and batch admission:** LeanAPI subscriptions,
  consolidator callbacks queuing observations and bar-close facts, host.on_data,
  EventBatch construction, services.apply, and session.process. Separate raw
  platform callbacks from completed data admitted to nodes.
- **7.3 Warmup, calendars, corporate actions, and cleanup:** history prefill,
  forward-only native indicators, regular/extended sessions, UTC versus exchange
  local time, DST/holidays, universes, typed splits/dividends, symbol changes,
  delistings, resource resets, and evidence for native boundary behavior.

Sources: `sugarloaf/adapters/quantconnect/README.md`, `main.py`, `launcher.py`,
`host.py`, `services.py`, `lean.py`, `tests/test_adapter_quantconnect.py`,
[validation](../../../../docs/VALIDATION.md).

## 8 Portfolio composition

- **8.1 Forecast aggregation and signal-to-position rules:** EMASpread,
  RSIReversion, SMATrend, MeanViewAggregator, and PositiveSignalPositions.
  Explain exact-target compatibility, matching calibration/event definitions,
  retained lineage, and what the baseline converter does not support.
- **8.2 Branch budgets, netting, and attribution:** PositionNetter, shared
  instruments, explicit scopes, account-level weight contributions, and branch
  attribution. Walk contributions of 20% plus 5% in one asset.
- **8.3 Constraints, explicit exits, weights, and quantities:**
  PortfolioConstraintNode and PortfolioConstraints; gross/net limits, position bounds,
  turnover, feasibility rejection, holdings retained outside explicit targets,
  explicit zeros, and active weights versus frozen quantities. Graph constraints
  and execution constraints are independently configured.

Sources: [node rules](../../../../sugarloaf/nodes/README.md),
`sugarloaf/nodes/technical.py`, `simple.py`, `positions.py`,
`sugarloaf/portfolio/constraints.py`, `tests/test_simple_nodes.py`,
`tests/test_technical_nodes.py`, `tests/test_execution.py`.

## 9 Orders and execution

- **9.1 TradingSession and target reconciliation:** process → accept → reconcile;
  exactly one PositionView; ExecutionPolicy, ExecutionState, IntentState,
  OrderIntent, and OrderUpdate. Desired quantity minus holdings minus pending
  quantity; valuation, lot rounding, fresh account reads, and target staleness.
- **9.2 Risk checks, pending orders, and submission:** RiskGate.check;
  exposure/buying power/borrow/session/capability checks, minimum trade value,
  sells-first submission, broker open orders plus local reservations without
  double-counting, intent identity saved before gateway.submit.
- **9.3 Partial fills, cancellation, rejection, and uncertain outcomes:**
  on_order_update and cumulative fills; host.on_order_event/_order_update/
  _drain_feedback; synchronous callbacks and reentrancy. Apply every queued update
  before reconciliation. Cancel-request acceptance differs from terminal
  cancellation; rejected/unknown submissions need deliberate treatment. Walk a
  50-share target with 20 filled and a superseding target yourself.

Sources: [execution contracts](../../../../docs/technical/EXECUTION.md),
`sugarloaf/execution/contracts.py`, `engine.py`, `session.py`,
`sugarloaf/adapters/quantconnect/host.py`,
`sugarloaf/adapters/memory/gateway.py`, `tests/test_execution.py`.

## 10 Predictive research and methodology

- **10.1 run_node and historical replay:** ResearchProvider, fresh node/provider,
  prepare/history_batches, ResearchRun, QuantBookServices, the same causal runtime
  without execution, and historical receipt assumptions. Walk the EMA notebook's
  construction and public run_node/evaluate usage rather than requiring remote
  execution. State the environment needed for that provider.
- **10.2 Forecast archives and outcome resolution:** ForecastArchive.record,
  immutable issued predictions, exclusion of PositionViews, replacement not
  erasing issued history, ReturnOutcomeResolver, exact endpoints, as_of,
  availability, and pending/unavailable/unsupported labels.
- **10.3 Metrics, coverage, and evaluation discipline:** evaluate,
  EvaluationReport.coverage/summary, family-specific metrics and baselines,
  cohort compatibility, retrospective forecasts, overlapping dependent horizons,
  chronological training/calibration/evaluation, and full trial provenance.
  Separate signal quality from net P&L, costs, execution, and market exposure.
  Descriptive metrics are not independent-sample statistical validation.

Sources: [research contracts](../../../../docs/technical/RESEARCH.md),
`sugarloaf/research/runner.py`, `archive.py`, `evaluation.py`,
`sugarloaf/adapters/quantconnect/research.py`, `examples/ema_research.ipynb`,
`tests/test_research_runner.py`, `tests/test_research_evaluation.py`,
`tests/test_research_actions.py`.

## 11 Deployment and evidence tooling

- **11.1 Deployment factories and manifests:** Deployment, Environment,
  graph_factory and fresh nodes; module:function factory; ticker bindings;
  strategy versus environment/execution config; explicit include sources, ancestor
  package initializers, notebook inclusion, and native entrypoint mode. Start with
  the public EMA factory/manifest, then explain adapting them for the learner's
  own strategy; a separate strategy repository is optional.
- **11.2 Packaging and cloud operations:** BundleSpec, read_spec/build_bundle,
  source/import rewriting, offline package inspection, project selection,
  upload/compile/compile-status/backtest/status/logs, and the remote notebook runner.
  Explain prerequisites and available connectors without assuming access or
  executing outward operations during teaching.
- **11.3 Reproducible identity and result retrieval:** actual source/config hashes,
  saved immutable snapshots, mutable selection pointers, compile/backtest IDs,
  submission intent before remote calls, interrupted responses, historical
  run_snapshot lookup, log pagination, SUGARLOAF_RUN/SUGARLOAF_RESULT, engine/data
  provenance, completeness, and durable retention outside ignored Git artifacts.

Sources: [external factories](../../../../docs/EXTERNAL_STRATEGIES.md),
`deployments/quantconnect/ema.toml`, `config.py`,
`sugarloaf/deployment/config.py`, `bundle.py`, `cloud.py`, `evidence.py`,
`research.py`, `reporting.py`, `tests/test_deployment_bundle.py`,
`tests/test_deployment_evidence.py`.

## 12 Maintenance and debugging

- **12.1 Tracing diagnostics and regression checks:** runtime and execution
  diagnostics; NotReady versus unsupported capabilities versus contract errors;
  choosing numerical, controlled-time, causal replay, or fill/cancel regressions.
  Demonstrate locating a failure from its event batch through the affected caller.
- **12.2 Local checks, CI, and native validation:** pytest/Ruff, CI interpreter
  matrix from the current workflow, source/config identity, and local fixtures
  versus real Python.NET/native APIs. Compile success differs from a completed
  error-free backtest, which still differs from economic validity.
- **12.3 Guided change walkthrough and operational limits:** explain where to
  change a signal, allocation, order policy, risk gate, reconciliation, or adapter
  capability and which checks would support it; no coding assignment. Explain
  current equity/order support, unverified replay/parity, absent durable restart
  recovery, and retention limits using current validation. Preserve dependency,
  commit/push, compute, release/live/capital, and external-action gates.

Sources: [validation](../../../../docs/VALIDATION.md),
`sugarloaf/core/errors.py`, `docs/GUIDE.md`, `tests/`,
`.github/workflows/checks.yml`, `AGENTS.md`.

## Essential traces to connect across sections

Teach these progressively when their components appear, not all on the first turn:

```text
initialize → load deployment → launcher.start → QuantConnectHost
→ services + gateway → ExecutionEngine + TradingSession + GraphRuntime

native consolidator → queued observations → host.on_data → EventBatch
→ TradingSession.process → GraphRuntime.process → provider.apply
→ ready nodes evaluate → final PositionView → engine.accept/reconcile
→ fresh broker state → RiskGate.check → intent reservation → gateway.submit

native order callback → host.on_order_event → cumulative ticket feedback
→ drain all updates → engine.on_order_update → engine.reconcile

run_node → fresh node + GraphRuntime → historical batches
→ ForecastArchive → later OutcomeResolver → evaluate → coverage and metrics
```

Treat these as orientation chains; show the current implementation's actual
branches, ordering, and error paths when explaining a concrete batch.
