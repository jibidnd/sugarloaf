# Research

Research on prices happens in the QuantConnect research kernel with `QuantBook` directly: a
study reads `qb.history` DataFrames and works on them with ordinary Python. A Sugarloaf node
is replayed only by a backtest. A graph whose output port emits predictive views runs as a
forecast run: the runner archives every view the nodes issue, and at the end of the
backtest the launcher scores each archived view against what prices then did
(`sugarloaf.research.view_metrics`) and writes the metrics to the backtest log. The caller
reads them through the REST API and saves them on disk.

Source: [archive](../../sugarloaf/research/archive.py),
[serialization](../../sugarloaf/research/serialization.py),
[anchors](../../sugarloaf/research/anchors.py),
[evaluation](../../sugarloaf/research/evaluation.py),
[view metrics](../../sugarloaf/research/view_metrics.py),
[inference](../../sugarloaf/research/inference.py).
Example: [EMA notebook](../../examples/ema_research.ipynb) and its
[walkthrough](../../examples/EMA_RESEARCH.md).

## From node to scores

1. Build a forecast graph: the node alone, with its view port as the graph output
   (`build_forecast_graph` in [examples/ema.py](../../examples/ema.py)).
2. Run it as a backtest from a manifest
   ([ema_forecasts.toml](../../deployments/quantconnect/ema_forecasts.toml)):
   `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema_forecasts.toml`.
   At the end of the backtest `launcher.finish` scores the archive and writes
   `VIEW_METRICS`, `VIEW_SAMPLE` and `RUN_SUMMARY` to the log
   ([run output](RUNTIME.md#run-output)); `run` saves them as
   `artifacts/ema_forecasts/<backtest_id>-output.json`.
3. Read a run's output later, locally:

<!-- snippet: fragment (needs QuantConnect credentials and a finished backtest) -->
```python
output = read_run_output(default_client(), project_id, backtest_id)
save_run_output(output, Path("artifacts/runs") / f"{backtest_id}.json")
output["metrics"]["counts"], output["metrics"]["kinds"]["score"]["pooled"]
```

The notebook has the complete code with imports.

## View metrics of a run

`view_metrics(records, prices, as_of)` resolves each record's target with
`ReturnOutcomeResolver` over a `PriceTable` of exact bar-end closes, scores it, and returns
`{"as_of", "path_fractions", "counts", "by_node_counts", "kinds", "unsupported_reasons"}`.
In a backtest `as_of` is the clock in `on_end_of_algorithm`, past every horizon it scores, and
the closes come from `adapter.history` at each instrument's subscription resolution:
split-adjusted for a `price_return` target, adjusted for splits and dividends for a
`total_return` target, split-adjusted for an `abs_log_close_return` target. The outcome is
computed from the closes of the bars ending at the target interval's start and end
(`evaluation.PRICE_OUTCOMES`): the exact simple return for quantity `return`, the absolute
log return for quantity `move_size`; no other price stands in for a missing one.

`counts` accounts for every issued view: `scored`, `horizon_after_end` (the target ends after
`as_of`), `no_bar` (no bar ends at a start or end time; `no_bar_at_start` and `no_bar_at_end`
say which, `no_bar_ends` and `horizon_after_end_ends` give the first and last target end) and
`unsupported` (no metric applies; `unsupported_reasons` lists the commonest). `kinds[kind]`
holds a `pooled` summary and one per node (`by_node`):

| Kind | Views | Summary |
| --- | --- | --- |
| `distribution` | A scalar `Distribution` | `pinball` (mean pinball loss at 0.1..0.9) and `pinball_mean`; `pit_histogram` (counts of the forecast cdf at the outcome in ten equal bins; uniform counts mean calibrated); `coverage_50` and `coverage_90` (share of outcomes inside the central 50% and 90% intervals); `log_score_mean` (mean negative log density, finite values) with `log_score_count` and `log_score_infinite`, where the law states a density; `quintiles` by forecast median: `count`, outcome `mean` and `median`, `forecast_median_mean`, `path` |
| `score` | `Score`, or an `Estimate` of the mean | `hit_rate` (sign of score against sign of outcome, over `hit_count` rows where both are nonzero); `precision`, `recall` and `f1` with up as the positive class; `deciles` by score: `count`, score range and mean, outcome `mean`, `median`, `q0.1`, `q0.25`, `q0.5`, `q0.75`, `q0.9`, `path`; `decile_spread` (top minus bottom decile mean) |
| `ranking` | `Ordering` | `kendall_tau_mean` and `kendall_tau_count` |
| `probability` | `Probability` | `brier_mean`, `log_loss_mean` |

`path` is the group's mean realized return from signal time to each of ten evenly spaced
times up to the target end (`path_fractions`), each read from the last close at or before
that time. Floats carry six significant digits; an infinite value is the string `"inf"`.
The pooled diagnostics (`pinball_loss`, `quantile_metrics`, `pit_histogram`,
`direction_metrics`, `quantile_groups`, `grouped_outcomes`) are in `evaluation`. Overlapping
horizons make the rows dependent; no sampling distribution is attached to these numbers.

## Forecast archive

`ForecastArchive.record(node_id, port, views, received_at)` is called by the runner for every
emission of a forecast run outside warm-up. Each `ForecastRecord` holds the original `view`,
`node_id`, `port` and `received_at`, the platform time of its first emission.

The archive keys on `view.metadata.view_id`. A repeated unchanged identity adds nothing; a
changed one raises `ContractError`. Replacement, withdrawal and expiry erase nothing.
Position views are actions and are skipped. Iterate the archive, take `len(archive)`, or
call `archive.get(view_id)`. It lives in memory for the length of the run.

`encode_archive(archive)` and `decode_archive(records)` convert it to and from JSON-ready
lists. `Estimate`, `Probability`, `Score`, `Ordering`, `QuantileDistribution` and
`ScaledHalfT` encode field by field; any other statement is pickled and base64-encoded, so decoding it needs its class
importable where it is decoded. `VIEW_SAMPLE` log lines carry this encoding of the first
views of each node.

## Outcome anchors

`ReturnOutcomeResolver` asks an anchor for the price of an instrument at an exact time.
`anchor_from_closes({instrument: {bar end: close}})` builds one from a table of closes; a
time with no close is unavailable, never replaced by a neighbouring price.
`closes_from_frame(frame, instrument_of, time_of)` builds that table from a `qb.history`
DataFrame indexed by (symbol, time) with a `close` column: `instrument_of` maps the
frame's symbol to the archive's `InstrumentId` and `time_of` maps its time index to an aware
bar end. One price table carries one adjustment convention; the caller chooses it to match
the forecasts' targets.

## Outcomes and evaluation

`evaluate(archive, resolver, *, as_of, metric_fn=default_metrics)` returns an
`EvaluationReport` with one `EvaluationRow` per forecast (`forecast`, `outcome`, `metrics`,
`status`, `reason`), `coverage` and `summary(pool_horizons=False)`. Outcome data stay in
evaluation; they never reach a node.

`OutcomeResolver.resolve(target, *, as_of) -> Outcome` is the extension point. A resolved
`Outcome` carries the exact target, a finite value or tuple, an availability time at or
after the target end and at most `as_of`, and provenance. The other statuses are `pending`,
`unavailable` and `unsupported`, each with a reason and no value. One label is resolved per
exact target and shared by its revisions. A forecast first received after `as_of` is pending.

`ReturnOutcomeResolver(anchor, *, provenance=None)` resolves decimal simple price or total
returns, joint coordinates and aligned return differences. Its anchor
`anchor(instrument, at, convention, as_of)` returns an `Observation` stamped at `at`, or
None. It computes end over start minus one with positive start and nonnegative end values.
An unmatured horizon is pending; a missing or later-available anchor is unavailable.

`metric_fn(view, outcome)` returns named floats or None and may extend
`default_metrics(view, outcome)`. Unsupported metrics leave an unscored row; NaN is
rejected.

## Default metrics

| View | Metrics and baseline |
| --- | --- |
| Return / relative return | Predicted minus realized error, absolute and squared error, error over the stated error scale; zero-return squared-error baseline |
| Probability | Separate events: binary Brier and log loss with 0.5 baseline. Exhaustive partition: categorical Brier and log loss with uniform baseline |
| Ranking | Kendall tau-b, concordance of strict predicted pairs, top-minus-bottom realized return, within-tie spread; random-order baseline |
| Distribution | Negative log density (scalar or joint), or exhaustive event-mass Brier and log loss; scalar PIT when the law has a CDF |
| Score | Raw score and outcome; cohort Pearson and Spearman association with at least three finite, nonconstant pairs |

Events are lower-open and upper-closed. Realized ties give strict ranking pairs half
credit. Impossible observed events keep infinite log loss.

## Cohorts and inference

Summary cohorts keep apart forecasts that differ in view family, node and model, target
meaning, units and convention, horizon, and lead time from receipt to target end; they also
keep score calibration ids, event definitions and distribution classes apart.
Session-count horizons vary in calendar length, so `summary(pool_horizons=True)` keeps one
daily signal in one cohort. Forecasts received at or after their target end are flagged as
retrospective.

`block_bootstrap_interval(values, block=..., seed=...)` gives a confidence interval for a
statistic of a time-ordered series by resampling blocks of consecutive observations; pass a
`(T, k)` array to resample several series on the same dates. `cross_sectional_ic(rows)`
computes one Spearman rank correlation per decision time across instruments. Overlapping
horizons and revisions are dependent observations: the summaries carry no independent-sample
p-values, and both inference tools describe predictive association before costs.

Tests: [evaluation](../../tests/test_research_evaluation.py),
[inference](../../tests/test_research_inference.py). The archive round trip through the
launcher is verified by the EMA forecast backtest in
[validation](../VALIDATION.md#native-checks).
