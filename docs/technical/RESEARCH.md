# Predictive-node research

Research runs the actual node with fresh state, archives its predictive Views,
then resolves outcomes separately. It requires no trading graph, allocation rule
or brokerage account. The notebook entry point is
[examples/ema_research.ipynb](../../examples/ema_research.ipynb); general authoring
belongs in [GUIDE.md](../GUIDE.md), runtime semantics in [RUNTIME.md](RUNTIME.md), and
QuantBook capabilities in [ADAPTERS.md](ADAPTERS.md).

## Replay and provenance

Public imports come from `sugarloaf.research`:

```python
from sugarloaf.adapters.quantconnect import QuantBookServices
from sugarloaf.research import ReturnOutcomeResolver, evaluate, run_node

services = QuantBookServices(qb, tickers=instrument_tickers)
run = run_node(
    node_factory, services, start=start, end=end,
    node_id="signal", configuration=experiment_parameters,
)
resolver = ReturnOutcomeResolver(exact_anchor, provenance={"dataset": "version-1"})
report = evaluate(run.archive, resolver, as_of=evaluation_time)
```

[`run_node`](../../sugarloaf/research/runner.py) constructs one node from the
factory and a one-node GraphRuntime with no execution. Its optional `output`
selects the designated port when the node exposes several; all predictive ports
are archived. Nodes with required upstream inputs need an explicit graph. Use a
fresh node and provider per experiment.

Runtime construction installs declarations. The runner calls
`services.prepare(start,end)`, then processes each `history_batches(start,end)`
batch. Loading/prefilling history establishes readiness without issuing past
decisions. Only admitted data with observation end <= graph time and availability
<= receipt time can enter node Context. Late arrivals do not rewind graph time.
QC owns native indicator state/windows; provider initialization and forward-only
update conventions are part of the experiment.

`ResearchRun` exposes `archive`, `runtime` and `provenance`. Provenance contains
node/provider classes, start/end, supplied configuration, provider details and
the historical-availability assumption. Runtime retains readiness/failure
diagnostics. Configuration is supplied by the caller, not automatically recovered
from arbitrary node state. Preserve data/configuration/model/provider versions
alongside exported results; historical receipt assumptions do not measure live
vendor latency.

## Issued forecasts

[`ForecastArchive.record(node_id, outputs, batch)`](../../sugarloaf/research/archive.py)
is the `GraphRuntime.on_output` callback, called after validated emissions. Each
`ForecastRecord` contains the original View, `node_id`, `port`, `batch_sequence`,
`graph_time` and `received_at` from its first emission.

The archive keys on `view.metadata.view_id`. Unchanged repeated identities add no
observation; changed contents under an existing identity raise ContractError.
Replacement, withdrawal and expiry never erase issued forecasts. PositionViews
are actions and are excluded. Iterate the archive, call `len(archive)`, or retrieve
`archive.get(view_id)`. Original target intervals and Metadata.cutoff remain
unchanged; receipt provenance is stored beside them. Storage is in-memory and
unbounded, not durable deployment recovery.

## Outcomes and extensions

[`evaluate`](../../sugarloaf/research/evaluation.py) accepts an archive/record
iterable, a resolver, explicit aware `as_of`, and optional `metric_fn`. It returns
an `EvaluationReport` with row-level forecast/outcome/metrics/status/reason,
`coverage`, and `summary()`. Evaluation data never become strategy inputs.

`OutcomeResolver.resolve(target, *, as_of) -> Outcome` is the extension contract.
A resolved Outcome preserves the exact Target, a finite scalar or tuple value,
availability at/after target end, and optional provenance. Its availability must
not exceed evaluation `as_of`. The other statuses are `pending`, `unavailable`
and `unsupported`; each requires a reason and has no invented label. A target's
label is resolved once per evaluation, shared by forecasts of that exact target.
Forecasts not yet issued at `as_of` remain pending.

`ReturnOutcomeResolver(anchor, provenance=None)` supports decimal simple
price/total returns, joint coordinates, and aligned scalar return differences.
Its callback `anchor(instrument, at, convention, as_of)` supplies an exact-time
`Observation` or None. Joint labels preserve subject/interval order. It computes
end/start minus one, requiring positive start and nonnegative end values, and
retains the latest endpoint availability. An unmatured horizon stays pending;
missing or not-yet-available anchors stay unavailable. It never substitutes a
nearby close, shifts a weekend horizon, or invents corporate-action adjustments.
A forecast whose horizon ends where no price exists therefore cannot be scored:
issue horizons in trading sessions (see `sugarloaf.nodes.technical.horizon_end`)
so a Friday forecast ends at Monday's close rather than on Saturday.
The source owns correct convention and point-in-time availability.

Custom `metric_fn(view, outcome)` returns named floats or None and can merge
`default_metrics(view, outcome)`. Unsupported metrics raise CapabilityError and
retain an unscored row. Wrong targets, future labels and malformed metrics fail
explicitly. Undefined metrics use None; NaN is rejected and infinities retained.

## Default diagnostics

| View | Metrics and baseline |
| --- | --- |
| Return / relative return | Predicted-minus-realized error, absolute/squared error, error divided by stated forecast-error scale; zero-return squared-error baseline |
| Probability | Separate events: mean binary Brier/log loss with 0.5 baseline. Exhaustive partition: summed categorical Brier/log loss with uniform baseline |
| Ranking | Kendall tau-b, concordance across strict predicted pairs, top-minus-bottom realized return and within-predicted-tie spread; random-order concordance baseline |
| Distribution | Negative scalar/joint log density, or exhaustive event-mass Brier/log loss; scalar PIT when the law exposes a CDF |
| Score | Raw score and outcome; cohort Pearson/Spearman association with at least three finite, nonconstant paired observations |

Events are lower-open and upper-closed. Realized ties give strict ranking pairs
half credit. Impossible observed events retain infinite log loss. Joint densities
retain dependence; a declared dimension must match the outcome. Continuous log
scores can be negative and depend on units/dimension. No KL to an empirical point
mass is used. Raw scores are not silently calibrated into returns. Model-specific
forecast losses and calibration belong to the external node implementing them.

## Comparisons and evidence limits

Summary cohorts preserve View family, node/model identity, target meaning and
units/convention, full horizon, and actual receipt-to-target lead time. They also
retain score calibration identity, event definitions or distribution class as
applicable. Ranking cohorts identify the universe independently of forecast order.
Different lead times remain separate; calendar dates with matching semantics can
pool. Session-count horizons vary in calendar length (one day mid-week, three
over a weekend), so pass `summary(pool_horizons=True)` to keep one daily signal
in one cohort. Each cohort reports metric means, count, distinct targets and infinite-score
count. Probability/event-mass cohorts provide ten reliability bins, with 1 in the
final bin. Undefined means remain None; infinities are not silently dropped.

Forecasts received at/after target end remain visible and are flagged as
retrospective, including in coverage and summaries. They are not prospective
evidence. Overlapping horizons and forecast revisions are dependent observations;
these descriptive summaries supply no independent-sample p-values. The experiment
owns chronological training/validation splits, calibration and baseline selection.

[`sugarloaf.research.inference`](../../sugarloaf/research/inference.py) adds two
tools for judging a result:

- `block_bootstrap_interval(values, block=..., seed=...)` returns a confidence
  interval for a statistic of a time-ordered series. It resamples whole blocks
  of consecutive observations so serial dependence is preserved; choose a block
  at least as long as the forecast horizon in periods. Pass a `(T, k)` array to
  resample several series on the same dates, for example to compare two signals.
- `cross_sectional_ic(report.rows)` computes one Spearman rank correlation per
  decision time across instruments (the information coefficient), from forecasts
  that may come from one node per instrument. Feed its `ic` series to the
  bootstrap for an interval around the mean IC.

Both describe predictive association before costs; neither is a trading result. Unsupported target forms need an explicit resolver or
metric, rather than an implicit reinterpretation.

[Research tests](../../tests/test_research_evaluation.py) cover identity retention,
exact labels, causal availability, all six families, ties and joint density
dependence; [runner tests](../../tests/test_research_runner.py) exercise public
replay. Completed platform checks and unverified capabilities belong in
[VALIDATION.md](../VALIDATION.md). Keep each node's idea/source, adaptations,
experiments, unsuccessful findings and tests beside its implementation. Numerical
correctness and successful replay do not establish predictive skill or alpha.


## Implementation map

| Module | Entry points |
| --- | --- |
| [runner.py](../../sugarloaf/research/runner.py) | ResearchProvider, ResearchRun, run_node |
| [archive.py](../../sugarloaf/research/archive.py) | ForecastRecord, ForecastArchive.record/get |
| [evaluation.py](../../sugarloaf/research/evaluation.py) | Outcome, ReturnOutcomeResolver, evaluate, default_metrics, EvaluationReport |
| [QC research provider](../../sugarloaf/adapters/quantconnect/research.py) | prepare, history_batches, anchor_observation |

Follow runner → runtime → archive for issuance, then resolver → evaluation for labels
and metrics. The [API reference](../api/index.md) supplies current signatures.
