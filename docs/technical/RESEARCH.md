# Research

Research on prices happens in the QuantConnect research kernel with `QuantBook` directly: a
study reads `qb.history` DataFrames and works on them with ordinary Python. A Sugarloaf node
is replayed only by a backtest. A graph whose output port emits predictive views runs as a
forecast run: the runner archives every view the nodes issue, and the launcher saves the
archive to the Object Store. A notebook in the same project reads the archive back, reads
the prices it needs with one `qb.history` call, and scores the forecasts with
`sugarloaf.research.evaluate`.

Source: [archive](../../sugarloaf/research/archive.py),
[serialization](../../sugarloaf/research/serialization.py),
[anchors](../../sugarloaf/research/anchors.py),
[evaluation](../../sugarloaf/research/evaluation.py),
[inference](../../sugarloaf/research/inference.py).
Example: [EMA notebook](../../examples/ema_research.ipynb) and its
[walkthrough](../../examples/EMA_RESEARCH.md).

## From node to scores

1. Build a forecast graph: the node alone, with its view port as the graph output
   (`build_forecast_graph` in [examples/ema.py](../../examples/ema.py)).
2. Run it as a backtest from a manifest
   ([ema_forecasts.toml](../../deployments/quantconnect/ema_forecasts.toml)):
   `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema_forecasts.toml`.
   The run log's `SUGARLOAF_RESULT` line names the key
   `sugarloaf/results/<project_id>/<backtest_id>`; the archive is `forecasts.json` under it
   and the run summary `result.json`.
3. In the project's research kernel:

<!-- snippet: fragment (needs the QuantConnect research kernel and a saved archive) -->
```python
archive = decode_archive(json.loads(qb.object_store.read(KEY)))
frame = qb.history([symbol], start, end, Resolution.DAILY)
closes = closes_from_frame(frame, instrument_of=..., time_of=...)
resolver = ReturnOutcomeResolver(anchor_from_closes(closes), provenance={"source": "qb.history"})
report = evaluate(archive, resolver, as_of=evaluation_time)
report.coverage, report.summary(pool_horizons=True)
```

The notebook has the complete code with imports.

## Forecast archive

`ForecastArchive.record(node_id, port, views, received_at)` is called by the runner for every
emission of a forecast run outside warm-up. Each `ForecastRecord` holds the original `view`,
`node_id`, `port` and `received_at`, the platform time of its first emission.

The archive keys on `view.metadata.view_id`. A repeated unchanged identity adds nothing; a
changed one raises `ContractError`. Replacement, withdrawal and expiry erase nothing.
Position views are actions and are skipped. Iterate the archive, take `len(archive)`, or
call `archive.get(view_id)`. It lives in memory for the length of the run.

`encode_archive(archive)` and `decode_archive(records)` convert it to and from JSON-ready
lists. `Estimate`, `Probability`, `Score`, `Ordering` and `QuantileDistribution` encode field
by field; any other statement is pickled and base64-encoded, so decoding it needs its class
importable in the kernel.

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
[inference](../../tests/test_research_inference.py),
[archive round trip through the launcher](../../tests/test_quantconnect_adapter.py).
