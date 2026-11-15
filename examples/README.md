# Examples

The examples teach mechanics; none establishes an edge.

## Run the local example

From the repository root:

```powershell
uv run --cache-dir .uv-cache python -m examples.memory
```

Expected output: `{'holdings': 50.0, 'orders': 1, 'diagnostics': 1}`.

[`memory.py`](memory.py) builds two `FixedContribution` nodes, each with a `Schedule`, nets
them with `PositionNetter`, checks the target with `PortfolioConstraintNode` and runs the
graph on `MemoryAdapter`. `run()` fires the first schedule (the netter raises `NotReady`
because the second branch is missing: the one diagnostic), fires the second (one order for
50 shares, filled at the USD 100 mark) and fires the second again (a new target with the same
weights; no order). Read `FixedContribution.on_scheduled`, `build_graph` and `run` in that
order.

## Implementations

| Implementation | What it shows | Assumptions and limits |
| --- | --- | --- |
| [`ema.EMAScore`](ema.py) | One `Bars` input and two native EMAs; `update` reads them and emits a `ScoreView` of fast / slow − 1 | Raw prices; the score is uncalibrated; the horizon is a parameter |
| [`ema.LongWhenPositive`](ema.py) | Score to long/cash target | Fixed account-NAV weight |
| [`ema.build_graph`](ema.py) / [`ema.build_forecast_graph`](ema.py) | The same node in a trading graph and in a forecast graph | |
| [`memory.FixedContribution`](memory.py) | A schedule-driven node with no data inputs; branch netting | Synthetic mark and fills |

The [built-in nodes](../sugarloaf/nodes/README.md) hold reusable baseline rules.

## On QuantConnect

The [public factories](../deployments/quantconnect/config.py) run `EMAScore` on SPY with a
five-minute consolidation of the minute feed and a one-day horizon:
[ema.toml](../deployments/quantconnect/ema.toml) trades it,
[ema_forecasts.toml](../deployments/quantconnect/ema_forecasts.toml) archives its scores.
The [EMA walkthrough](EMA_RESEARCH.md) and [notebook](ema_research.ipynb) score a saved
archive in the research kernel.

```powershell
sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml
sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

`package` is offline. `run` needs QuantConnect credentials and a selected project; see
[deployment](../docs/technical/DEPLOYMENT.md). The native status of these runs under the
current design is in [validation](../docs/VALIDATION.md#redesign-of-14-november-2026).
