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
   The run saves `forecasts.json` under the key printed in its `SUGARLOAF_RESULT` line.
2. In the same project's research kernel, open the uploaded notebook, set `KEY`, and run it.
   It decodes the archive (`decode_archive`), reads daily closes with one `qb.history` call,
   builds an anchor with `closes_from_frame` and `anchor_from_closes`, and calls `evaluate`
   with `ReturnOutcomeResolver`. An endpoint with no close stays unavailable.

The notebook's daily closes match targets that start and end at session closes. The public
deployments issue a score on every five-minute bar with a one-day horizon, so only the
scores issued at 16:00 New York have both endpoints at daily closes; the anchor reports the
others unavailable. Score such a run with minute history, or run the forecast graph with
`resolution="daily"` and an `int` horizon. Record datasets, parameter trials, held-out
periods and unsuccessful results here before making a research claim. Overlapping forecasts
are dependent observations.

**Validation.** The node, both graphs and the public deployments have local tests in
[tests/test_examples.py](../tests/test_examples.py). The archive round trip through the
launcher and both public deployments are verified by the backtests listed in
[validation](../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.
The notebook ships without outputs.
