# EMA spread node

Implementation: [ema.py](ema.py). Start with [ema_research.ipynb](ema_research.ipynb)
in QuantConnect Research, with the repository's Python modules available.
`sugarloaf upload --adapter quantconnect --manifest deployments/quantconnect/ema.toml` uses the public EMA manifest,
which explicitly includes the notebook and transforms its imports
alongside the Python files for QC Cloud's `types.py` filename restriction. Open that
uploaded copy when using Cloud; the local copy uses the repository's native imports.

**Idea:** a positive fast/slow EMA spread may predict positive subsequent returns.
This is a teaching hypothesis, not a replication of a specific academic paper or
evidence of alpha. The standalone node emits a ScoreView; it does not pretend the
spread is a calibrated expected return. Its optional graph adds a long/cash
allocation rule and account constraints using the same node.

**Source:** [QuantConnect's EMA implementation](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/exponential-moving-average).
We use LEAN's defaults and native output windows, prefilled from historical data.
The adapter records the implementation version and initialization details.

**Parameters:** fast/slow periods, source interval, forecast horizon and (for the
trading graph) target weight. Windows retain two indicator outputs. Source prices
are raw; outcomes use the exact price-return convention. Horizons are elapsed
calendar time, not trading-session offsets: a missing weekend/holiday anchor is
reported unavailable rather than silently replaced with another day's price.
Score calibration identity includes the EMA periods, bar cadence and raw-price
convention, so different score definitions are kept separate in aggregation and
evaluation. Evaluation requires both native points to share an admitted timestamp
and the slow EMA to be positive.

**Experiment:** the notebook runs a fresh node in QuantBook, archives issued scores,
resolves subsequent returns separately and prints coverage and score association
diagnostics. A one-period example is only a workflow demonstration. Document new
datasets, parameter trials, held-out periods and unsuccessful results here before
making a research claim. Framework diagnostics do not supply independent-sample inference for overlapping forecasts.

**Validation:** native indicator/resource and causal replay regressions live in
`tests/test_adapter_quantconnect.py`; archive/outcome/scoring cases live in
`tests/test_research_evaluation.py`; the public runner has an integration check in
`tests/test_research_runner.py`. Current Cloud acceptance evidence is in
[Validation](../docs/VALIDATION.md). Local fakes do not establish real QuantBook notebook
execution; the notebook itself is supplied without fabricated outputs.

**Current research behavior:** QuantBook replay admits typed split/dividend facts
at effective boundaries and resets affected data/indicator resources before
post-action observations. Split warnings are not effective split facts. Historical
receipt times are assumed equal to observation ends, not measured latency.
The native notebook issued nine forecasts and scored six during its recorded
January 2025 replay; three exact anchors were unavailable. Local tests verify
additional causal admission/reset cases. Native corporate-action replay,
numerical parity and point-in-time adjustment vintages remain unverified.
See [adapter replay](../docs/technical/ADAPTERS.md#quantbook-and-memory-providers).


The `technical/` examples are explicit batch-calculation fixtures.
Their rolling-window reseeding is not the native streaming indicator used here.
