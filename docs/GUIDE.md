# Getting started

This path runs locally in a few minutes with no QuantConnect account. It runs a complete
fixture strategy, writes a node, scores a forecast run, and points to the deployment
commands.

## 1. Run the memory example

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/), then
from the repository root:

```powershell
uv sync --cache-dir .uv-cache
uv run --cache-dir .uv-cache python -m examples.memory
```

Expected output:

```text
{'holdings': 50.0, 'orders': 1, 'diagnostics': 1}
```

[The example](../examples/memory.py) gives a USD 10,000 account and a USD 100 mark. Two
`FixedContribution` nodes each declare a schedule and, when it fires, request 20% and 30%
of NAV. `PositionNetter` sums them to 50% and `PortfolioConstraintNode` checks the result.
The first schedule fires alone: the netter raises `NotReady` (one branch is missing), which
is the one diagnostic. The second completes the target, and `MemoryAdapter.execute` buys and
fills 50 shares. The second branch fires again: the netter issues a new target with the
same weights, and `execute` finds the account already at 50 shares, so no order follows.

The objects involved: `Graph` wires nodes; `GraphRunner` runs them; `MemoryAdapter` plays
the platform. [Architecture](ARCHITECTURE.md) traces the calls.

## 2. Write a node

A node declares its data and ports as attributes and implements `update`. This one declares
daily bars and a native SMA, scores the close against the average, and feeds the built-in
`PositiveSignalPositions`. It runs as written from the repository root:

```python
from datetime import UTC, datetime, timedelta

from sugarloaf.adapters.memory import MemoryAdapter
from sugarloaf.core.data import Bar
from sugarloaf.core.errors import NotReady
from sugarloaf.core.graph import Graph
from sugarloaf.core.inputs import Bars, Indicator, Selection
from sugarloaf.core.node import OutputSpec
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import Score, ScoreView, return_target
from sugarloaf.execution import ExecutionPolicy
from sugarloaf.nodes.simple import PositiveSignalPositions
from sugarloaf.runtime import GraphRunner


class CloseOverSMA:
    """Score = latest close / SMA(3) - 1, issued on each daily bar."""

    ports = {}  # no upstream views
    outputs = {"views": OutputSpec((ScoreView,))}

    def __init__(self, instrument):
        self.instrument = instrument
        selection = Selection((instrument,))
        # Declarations, read once by the runner and set up on the adapter.
        self.bars = Bars(selection, "daily", window=3)
        self.inputs = (self.bars,)
        self.indicators = (Indicator("sma", "sma", 3, selection, "daily"),)
        self.config = {"period": 3}

    def update(self, ctx, data, views):
        bar = data.bars.get(self.instrument)
        if bar is None:
            return None  # nothing new for this instrument: outputs unchanged
        sma = ctx.indicator("sma", self.instrument)
        if not sma.is_ready:
            raise NotReady("SMA(3) is not ready")  # withdraws output, records a diagnostic
        end = ctx.time + timedelta(days=1)
        view = ScoreView(
            return_target(self.instrument, Interval(ctx.time, end)),
            Score(bar.close / sma.value - 1, "close-over-sma:3"),
            Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, end),
        )
        return {"views": (view,)}


asset = InstrumentId("fixture:equity:1")
graph = Graph().add_node("signal", CloseOverSMA(asset))
graph.add_node("positions", PositiveSignalPositions((asset,), budget=0.5))
graph.connect("signal", "views", "positions", "views")
compiled = graph.set_output("positions", "positions").compile()

start = datetime(2025, 1, 6, 21, tzinfo=UTC)  # 16:00 New York
adapter = MemoryAdapter(start, account=10_000)
runner = GraphRunner(compiled, adapter, policy=ExecutionPolicy(rebalance_band=0))
adapter.attach(runner)  # runner.start(): subscribe, add_indicator
for day, close in enumerate([100.0, 101.0, 102.0, 104.0]):
    end = start + timedelta(days=day)
    adapter.deliver(end, [Bar(asset, end - timedelta(days=1), end, close, close, close, close, 1e6)])

print(runner.last_target.positions[asset], adapter.holdings[asset], runner.diagnostic_count)
```

It prints `0.5 49.0 2`: the SMA(3) is not ready on the first two bars (two `NotReady`
diagnostics); on the third bar the score is positive, the target is 50% of NAV and the
adapter buys 49 shares at 102. On the fourth bar the target is again 50%: at 104 the exact
quantity is 48.55 shares, and selling one share would not bring the account closer to it, so
`order_quantity` returns zero.

The rules the example follows:

- Declarations (`inputs`, `indicators`, `schedules`, `warm_up`) are attributes; the runner
  reads them once in `runner.start()`, which `MemoryAdapter.attach` calls.
- `update` returns the complete output as `{port: views}`. Returning `None` withdraws every
  port; to keep the last output on a slice without news, return it again
  ([`EMAScore.update`](../examples/ema.py) does this).
- `NotReady` withdraws the node's outputs and records a diagnostic; downstream nodes see no
  view.
- View ids must be unique in the run: `f"{ctx.node_id}:{ctx.sequence}"` is.
- Weights are fractions of account NAV; an explicit zero is an exit.

On the memory adapter an indicator updates from bars of a feed declared with the same
resolution, interval and normalization, which is why the node declares both. On
QuantConnect the platform feeds and warms the indicator itself.
[Core](technical/CORE.md) lists every declaration and context method.

## 3. Research a forecast

A graph whose output port emits predictive views is a forecast run: the runner archives
every view and sends no orders. On QuantConnect the launcher saves the archive to the Object
Store and a research notebook scores it. The same steps run locally on the memory adapter:

```python
import json
from datetime import UTC, datetime, timedelta

from examples.ema import EMAConfig, build_forecast_graph
from sugarloaf.adapters.memory import MemoryAdapter
from sugarloaf.core.data import Bar
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.research import ReturnOutcomeResolver, evaluate
from sugarloaf.research.anchors import anchor_from_closes
from sugarloaf.research.serialization import decode_archive, encode_archive
from sugarloaf.runtime import GraphRunner

asset = InstrumentId("fixture:equity:1")
start = datetime(2025, 1, 6, 21, tzinfo=UTC)
closes = [100.0, 101.0, 103.0, 102.0, 104.0, 105.0, 104.0]
ends = [start + timedelta(days=k) for k in range(len(closes))]

# A forecast run: the graph's output is the score's view port, so nothing trades.
config = EMAConfig(fast=2, slow=3, horizon=timedelta(days=1))
adapter = MemoryAdapter(start)
runner = GraphRunner(build_forecast_graph(asset, config), adapter)
adapter.attach(runner)
for end, close in zip(ends, closes):
    adapter.deliver(end, [Bar(asset, end - timedelta(days=1), end, close, close, close, close, 1e6)])

# What the QuantConnect launcher saves as forecasts.json, read back as a research kernel would.
archive = decode_archive(json.loads(json.dumps(encode_archive(runner.archive))))
resolver = ReturnOutcomeResolver(anchor_from_closes({asset: dict(zip(ends, closes))}))
report = evaluate(archive, resolver, as_of=ends[-1])
print(len(archive), report.coverage["by_status"])
```

It prints `5 {'scored': 4, 'pending': 1}`: five scores were issued once both EMAs were
ready; four targets ended by `as_of` and were scored; the last one's horizon had not ended.
On QuantConnect the closes come from one `qb.history` read through `closes_from_frame`; the
[EMA notebook](../examples/ema_research.ipynb) and its
[walkthrough](../examples/EMA_RESEARCH.md) show it. [Research](technical/RESEARCH.md)
defines the archive, the resolver and the metrics. Research on prices themselves uses
`QuantBook` in the research kernel directly.

## 4. Deploy and inspect

A factory returns a `Deployment`: a graph factory, settings, ticker bindings, environment,
execution policy and constraints. A TOML manifest selects the factory and the source files.
The [public factories](../deployments/quantconnect/config.py) back two manifests:
[ema.toml](../deployments/quantconnect/ema.toml) (trading) and
[ema_forecasts.toml](../deployments/quantconnect/ema_forecasts.toml) (forecast run).

Inspect the bundle offline:

```powershell
uv run --cache-dir .uv-cache sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

This writes evidence under `artifacts/ema/` and contacts nothing. With QuantConnect
credentials and a project selected in the manifest or saved state,
`sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml` uploads,
compiles, backtests and retrieves results and logs; `sugarloaf create` with the same
arguments provisions a project once. In the logs, `SUGARLOAF_RUN` records the mode and
versions and `SUGARLOAF_RESULT` the Object Store key of the result file.
[Deployment](technical/DEPLOYMENT.md) documents the commands and the evidence kept.

A compile proves a build. A completed backtest proves the run completed, not that the
strategy has an edge. [Validation](VALIDATION.md) lists what has native evidence.

## 5. Where to change what

| Change | Where |
| --- | --- |
| A signal or an allocation rule | A node; [built-in nodes](../sugarloaf/nodes/README.md) are starting points |
| Which data a node receives | Its declarations ([core](technical/CORE.md#declarations)) |
| How targets become orders | `ExecutionPolicy` ([execution](technical/EXECUTION.md)) |
| Account limits | `PortfolioConstraintNode` in the graph and `Deployment.constraints` ([portfolio](technical/PORTFOLIO.md)) |
| A platform call | The adapter ([adapters](technical/ADAPTERS.md)) |
| A private strategy | An [external package](EXTERNAL_STRATEGIES.md) |

[Maintenance](MAINTENANCE.md) covers tests and diagnosis.
