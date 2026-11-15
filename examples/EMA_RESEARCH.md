# EMA spread node

Implementation: [ema.py](ema.py). Notebook: [ema_research.ipynb](ema_research.ipynb).

**Idea.** A positive fast/slow EMA spread may predict positive subsequent returns. This is a
teaching hypothesis, not a replication of a study or evidence of an edge. `EMAScore` emits a
`ScoreView`; the spread is not presented as a calibrated expected return.

**Node.** `EMAScore(instrument, config)` declares one `Bars` input at
`config.resolution` and `config.interval` with raw prices, and two `Indicator` declarations
(`ema` with periods `config.fast` and `config.slow`) on the same feed. The platform creates,
warms and updates both EMAs. On each completed bar `update` reads `ctx.indicator("fast", ...)`
and `ctx.indicator("slow", ...)`; when both are ready, stamped at the same time and the slow
value is positive, it emits `fast / slow − 1` with target interval `[ctx.time, horizon end]`.
A slice without the instrument's bar restates the previous output.

**Source.** [QuantConnect's EMA](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/exponential-moving-average),
created by `algorithm.ema` with `settings.automatic_indicator_warm_up = True`.

**Parameters.** `EMAConfig(fast=20, slow=50, resolution="daily", interval=None,
horizon=1, weight=0.2)`. A horizon given as an `int` counts trading sessions and ends at the
close of the last one (`horizon_end`, through `ctx.calendar(...).next_close`); a
`timedelta` is elapsed calendar time. The public deployments use a minute feed consolidated
to five minutes and `timedelta(days=1)`. The calibration id records the periods, the bar
period and the raw-price convention, so different definitions stay apart in aggregation and
evaluation.

**Experiment.**

1. Run the forecast manifest:
   `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema_forecasts.toml`.
   At the end of the backtest the launcher scores every archived view against the closes of
   SPY's minute bars (split-adjusted, from `adapter.history`) and writes `VIEW_METRICS` to
   the log; `run` saves the parsed output as
   `artifacts/ema_forecasts/<backtest id>-output.json`.
2. Open the notebook locally, set `BACKTEST`, and run it. It reads the run's log through
   the REST API (`read_run_output`), saves the output on disk (`save_run_output`) and prints
   the counts, the hit rate, precision, recall and F1, and realized returns by score decile
   with their path from signal time.

A score's target is the price return from its bar's close to `ctx.time + 1 day`. A target
ending on a day without a session (a Thursday score when Friday is a holiday, a Friday score
ending on Saturday) has no bar at its end and is counted under `no_bar`, not scored; a
target ending after the backtest is counted under `horizon_after_end`. An `int` horizon
(`horizon=1`) ends at the next session close instead. Record datasets, parameter trials,
held-out periods and unsuccessful results here before making a research claim. Overlapping
forecasts are dependent observations.

**Validation.** The node, both graphs and the public deployments have local tests in
[tests/test_examples.py](../tests/test_examples.py). The view metrics written by the
launcher and both public deployments are verified by the backtests listed in
[validation](../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.
The notebook ships without outputs.
