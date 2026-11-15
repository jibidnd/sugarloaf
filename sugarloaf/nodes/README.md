# Built-in nodes

| Node | Module | Rule |
| --- | --- | --- |
| `EMASpread` | [technical.py](technical.py) | Fast EMA / slow EMA − 1 |
| `RSIReversion` | [technical.py](technical.py) | (threshold − RSI) / 100; positive below the threshold |
| `SMATrend` | [technical.py](technical.py) | Completed close / SMA − 1 |
| `MeanViewAggregator` | [simple.py](simple.py) | Arithmetic mean of matching return, score or probability views |
| `PositiveSignalPositions` | [simple.py](simple.py) | Equal account-NAV weights among positive signals; explicit zeros for the rest |
| `PositionNetter` | [positions.py](positions.py) | Sum of budgeted branch targets, with attribution |
| `PortfolioConstraintNode` | [positions.py](positions.py) | Rejects infeasible account targets without rescaling |

These are transparent baselines, not calibrated models. Specialized models belong in
external packages.

## Indicator scores

`EMASpread(instrument, fast=20, slow=50, *, resolution="daily", interval=None, horizon=1)`,
`RSIReversion(instrument, period=14, threshold=30, ...)` and
`SMATrend(instrument, period=210, ...)` each declare one `Bars` input (adjusted prices) and
their native indicators on the same feed (`Indicator` kinds `ema`, `rsi`, `sma`). On every
completed bar of the instrument `update` reads the indicators through `ctx.indicator`,
raises `NotReady` until they are ready, and emits one `ScoreView` on port `views`. A slice
without the instrument's bar restates the previous output.

`horizon` is an `int` count of regular trading sessions (default 1) or a `timedelta`. A
session count ends at the close of the last session, read from `ctx.calendar(instrument)`:
from Friday's close, one session ends at Monday's close. Scores state direction, not
expected returns; the RSI rule holds no position state.

The example below compiles a trading graph; it runs as written:

```python
from sugarloaf.core.graph import Graph
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.nodes.simple import PositiveSignalPositions
from sugarloaf.nodes.technical import EMASpread

asset = InstrumentId("USA:SPY")
graph = Graph().add_node("signal", EMASpread(asset))
graph.add_node("positions", PositiveSignalPositions((asset,), budget=0.8))
graph.connect("signal", "views", "positions", "views")
compiled = graph.set_output("positions", "positions").compile()
assert compiled.emits_positions()
```

Substitute `RSIReversion(asset)` or `SMATrend(asset)` to change the signal. Running the
graph needs an adapter with the indicators (`MemoryAdapter` computes fixture versions) and,
for a session-count horizon, a calendar for the instrument.

## Mean aggregation

Port `views` in, port `views` out. Claims combine only with the same claim family and the
exact same target; scores also need the same calibration id, probabilities the same events
and exhaustiveness. Return means keep the largest input error scale. Duplicate identities
raise `ContractError`; outputs expire no later than their parents and target ends.

## Positive-signal positions

`PositiveSignalPositions(instruments, budget=0.9, scope="account", lifetime=timedelta(days=4))`
selects instruments whose return or score is positive, or whose `P(return > 0)` exceeds 0.5,
and divides `budget` equally among them; the rest get explicit zeros. It needs one current
claim per configured instrument with one horizon and convention; a missing claim raises
`NotReady`, which withdraws the target without liquidating.

## Netting and constraints

`PositionNetter(budgets)` and `PortfolioConstraintNode(constraints)` are described in
[portfolio](../../docs/technical/PORTFOLIO.md).

```python
from sugarloaf.core.graph import Graph
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.nodes.simple import MeanViewAggregator, PositiveSignalPositions
from sugarloaf.nodes.technical import RSIReversion, SMATrend

asset = InstrumentId("USA:SPY")
graph = Graph().add_node("trend", SMATrend(asset)).add_node("rsi", RSIReversion(asset))
graph.add_node("mean", MeanViewAggregator())
graph.add_node("positions", PositiveSignalPositions((asset,), budget=0.8))
graph.connect("trend", "views", "mean", "views")
graph.connect("rsi", "views", "mean", "views")
graph.connect("mean", "views", "positions", "views")
compiled = graph.set_output("positions", "positions").compile()
```

The two scores carry different calibration ids, so the aggregator keeps them as two groups
and `PositiveSignalPositions` refuses two claims for one instrument (`ContractError`). Use
this wiring with signals that share a calibration, or net separate branches with
`PositionNetter`.

Tests: [tests/test_nodes.py](../../tests/test_nodes.py),
[tests/test_view_axes.py](../../tests/test_view_axes.py). Signatures:
[API reference](../../docs/api/portfolio.md).
