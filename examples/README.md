# Examples

Start with the local memory example, then choose a predictive or native trading
path. Examples demonstrate declared mechanics; none establishes profitable alpha.

## Run a complete local path

From the repository root:

```powershell
uv run --cache-dir .uv-cache python -m examples.memory
```

[`memory.py`](memory.py) constructs two fixed-position branches, nets them and
checks constraints, then attaches execution. An explicit account/calendar/price
fixture supports a 50-share target. The caller supplies a 20-share partial fill,
replays the batch and supplies the remaining 30 shares. Expected output:
`{'holdings': 50, 'orders': 1, 'diagnostics': 0}`.

Read `FixedContribution.evaluate`, `build_graph` and `run` in that order.
This shows the full wiring without QC or historical data.

## Choose an example

| Implementation | What it teaches | Assumptions and limits |
| --- | --- | --- |
| [`ema.EMAScore`](ema.py) | Native fast/slow EMA ratio minus one as an uncalibrated ScoreView | QC indicators, initialization and horizon are explicit experiment assumptions |
| [`ema.LongWhenPositive`](ema.py) | Score-to-long/cash conversion | Fixed account-NAV weight; no expected-return calibration or cost optimization |
| [`memory.FixedContribution`](memory.py) | Overlapping branches, replay and partial/full fills | Synthetic prices, sessions and fills are supplied explicitly |
| [Technical examples](technical/README.md) | EMA crossover, RSI mean reversion and Donchian direct-position rules | Batch reference calculations, distinct from QC-native indicator resources |

The [built-in node package](../sugarloaf/nodes/README.md) contains reusable baseline
indicator, aggregation, allocation and position-composition rules.

## Native research and deployment

The [EMA walkthrough](EMA_RESEARCH.md) and [notebook](ema_research.ipynb) run the
same predictive node in QuantBook, archive issued scores and resolve outcomes
separately. The bundled notebook passed in the native QuantBook kernel for
January 6–18, 2025; see [validation](../docs/VALIDATION.md) for its scope and
retained output. The authored notebook keeps its output cells empty.

The [public factory](../deployments/quantconnect/config.py) and
[manifest](../deployments/quantconnect/ema.toml) launch its trading graph.
[Deployment tools](../docs/technical/DEPLOYMENT.md) package selected sources and
retain run identity. The offline `package` action requires no cloud account.
Use `sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml` to inspect the
bundle, then `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml` in an
authenticated LEAN environment with a selected QC project to backtest it.

Follow [getting started](../docs/GUIDE.md), [research](../docs/technical/RESEARCH.md)
and [validation](../docs/VALIDATION.md) for contracts and evidence limits.
