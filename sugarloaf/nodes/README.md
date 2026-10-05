# Generic strategy nodes

| Node | Rule |
| --- | --- |
| `EMASpread` | Fast EMA / slow EMA - 1 |
| `RSIReversion` | (Threshold - RSI) / 100; positive below the threshold |
| `SMATrend` | Completed close / SMA - 1 |
| `MeanViewAggregator` | Equal arithmetic means of matching return, score or probability Views |
| `PositiveSignalPositions` | Equal account-NAV weights among positive signals; zeros for the rest |
| `PositionNetter` | Sum explicitly budgeted branches, retaining attribution and explicit exits |
| `PortfolioConstraintNode` | Reject infeasible account targets without rescaling |

Import indicator nodes from `sugarloaf.nodes.technical`, aggregation and conversion
from `sugarloaf.nodes.simple`, and netting and constraints from
`sugarloaf.nodes.positions`. These are transparent baseline rules, not calibrated
alpha or a fitted world model. More specialized models belong in external packages.

## Barebones indicator nodes

Each node takes one instrument and emits a `ScoreView` on completed bars. The
adapter supplies native EMA, RSI or SMA values using adjusted prices. No node
implements its own rolling indicator. Defaults: EMA 20/50, RSI 14 with threshold
30, SMA 210; daily bars and a one-calendar-day forecast horizon.

Scores indicate direction, not calibrated expected returns. RSI is deliberately
stateless: positive below 30, zero at 30, negative above it. It does not hold until
a separate exit threshold. SMA trend emits on every bar; it does not include monthly scheduling or a multi-asset allocation policy. Change interval/horizon explicitly.

```python
from sugarloaf.core.graph import Graph
from sugarloaf.core.types import InstrumentId
from sugarloaf.nodes.technical import EMASpread
from sugarloaf.nodes.simple import PositiveSignalPositions

asset = InstrumentId("USA:SPY")
graph = Graph().add_node("signal", EMASpread(asset))
graph.add_node("positions", PositiveSignalPositions((asset,), budget=0.8))
graph.connect("signal", "views", "positions", "views")
compiled = graph.set_output("positions", "positions").compile()
```

Substitute `RSIReversion(asset)` or `SMATrend(asset)` to change the signal. Use the
resulting graph in an ordinary Deployment. These are starting points for research,
not claims of profitable trading rules. [Tests](../../tests/test_technical_nodes.py)
verify signal/position boundaries and alignment using supplied indicator observations.

## Mean aggregation

Connect forecasts to input `views`; output is also `views`. Only claims with the
same concrete View type and exact Target combine. Scores additionally require the
same calibration identity; probabilities require identical ordered event definitions
and the same exhaustive flag. Different groups remain separate. Return means are
averaged; the maximum input error scale is retained without claiming independent
evidence or an improvement in forecast confidence. That scale is not newly calibrated.
Duplicate identities fail instead of double-counting the same evidence. Parent
IDs are retained; outputs cannot outlive their earliest input expiry or target end.

## Naive positions

Connect aggregated Views to `PositiveSignalPositions(instruments, budget=0.9)`.
Return or score > 0 selects an asset; an explicit P(return > 0) > 0.5 also selects
it. Equality selects cash. The selected assets equally divide the configured
account-NAV budget; every other configured asset receives explicit zero. All-off
means an all-zero target. The score's zero must be meaningful for that strategy.

Require one claim per configured asset, aligned horizons and return conventions.
Missing evidence withdraws authority instead of liquidating. Multiple claim families
for the same asset must be resolved by the strategy or converted in separate
budgeted branches. Rankings, distributions and relative-return forecasts are not
converted by these baseline nodes. Weight targets can generate maintenance trades
while active; use quantity targets when a strategy must freeze monthly holdings.

## Graph wiring

```python
from sugarloaf.core.graph import Graph
from sugarloaf.nodes.simple import MeanViewAggregator, PositiveSignalPositions

# signal_a/signal_b publish compatible Views for the configured instruments.
graph = Graph().add_node("a", signal_a).add_node("b", signal_b)
graph.add_node("mean", MeanViewAggregator())
graph.add_node("positions", PositiveSignalPositions(instruments, budget=0.8))
graph.connect("a", "views", "mean", "views")
graph.connect("b", "views", "mean", "views")
graph.connect("mean", "views", "positions", "views")
compiled = graph.set_output("positions", "positions").compile()
```

[Baseline tests](../../tests/test_simple_nodes.py) cover alignment, lineage, expiry,
missing evidence, probability meaning and exits. [Execution tests](../../tests/test_execution.py)
cover netting and constraints. See [portfolio composition](../../docs/technical/PORTFOLIO.md)
and [external strategy deployment](../../docs/EXTERNAL_STRATEGIES.md).


## Implementation and maintenance

- [technical.py](technical.py): EMASpread, RSIReversion and SMATrend declare
  native requirements, then read ready outputs and issue ScoreViews.
- [simple.py](simple.py): MeanViewAggregator groups compatible claims;
  PositiveSignalPositions turns aligned positive evidence into explicit weights.
- [positions.py](positions.py): PositionNetter combines budgeted branches;
  PortfolioConstraintNode validates account feasibility.

The [API reference](../../docs/api/index.md) lists constructors/methods.
Read [market](../../docs/technical/MARKET.md) for native readiness,
[runtime](../../docs/technical/RUNTIME.md) for activation and
[portfolio](../../docs/technical/PORTFOLIO.md) for allocation semantics.
