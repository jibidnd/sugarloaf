# Getting started with Sugarloaf

This local path takes about 10–15 minutes in a configured Python environment.
Run a complete fixture strategy, build a small graph and locate the research and
deployment entry points. No QC account is needed for the local steps.

## Set up and run

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/),
then run from the repository root:

```powershell
uv sync --cache-dir .uv-cache
uv run --cache-dir .uv-cache python -m examples.memory
```

Expected output:

```text
{'holdings': 50, 'orders': 1, 'diagnostics': 0}
```

[The example](../examples/memory.py) supplies a USD10,000 account and a USD100
asset. Two branches request 20% and 30% account weights. `PositionNetter` combines
them into 50%; `PortfolioConstraintNode` checks the target; execution submits
50 shares. The caller fills 20 shares, replays the same batch, then fills 30.
Replay creates no second order. Fixture fills are explicit inputs.

The key objects are `Graph`, `GraphRuntime`, `TradingSession` and `ExecutionEngine`.
Nodes publish immutable `View` messages. The graph wires them; the runtime decides
when to evaluate them; the session hands a final `PositionView` to execution.
[Architecture](ARCHITECTURE.md) traces their interactions.

## 1. Implement a node

A node declares input/output ports, data requirements and triggers, then returns
a complete output snapshot from `evaluate(inputs, ctx)`. This minimal local
example reuses the fixed-position node:

```python
from datetime import datetime, timezone

from examples.memory import FixedContribution
from sugarloaf.adapters.memory import MemoryServices
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.graph import Graph
from sugarloaf.core.types import InstrumentId
from sugarloaf.runtime.runtime import GraphRuntime

asset = InstrumentId("fixture:equity:1")
graph = Graph().add_node("allocation", FixedContribution(asset, 0.25, "main"))
compiled = graph.set_output("allocation", "positions").compile()
runtime = GraphRuntime(compiled, MemoryServices())
now = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)
runtime.process(EventBatch(1, now, (Event("scheduled", key="rebalance"),)))
print(runtime.store.get("allocation", "positions")[0].positions[asset])  # 0.25
```

This evaluates allocation only. Execution also requires instrument, account,
valuation and calendar services, as the complete memory example shows.
Read [`FixedContribution.evaluate`](../examples/memory.py) for the actual
`Metadata`, `PositionView` and `NodeOutputs` construction.

Choose a View matching the claim: a raw signal belongs in `ScoreView`; desired
holdings belong in `PositionView`. Weights are fractions of account NAV.
Use unique View IDs, `ctx.node_id` as source and explicit expiry. Omitted output
ports withdraw their previous Views; explicit zero positions request exits.
[Core](technical/CORE.md) defines contracts; the [API reference](api/index.md)
documents individual interfaces.

## 2. Research the claim

Predictive research uses fresh node/runtime state without order execution.
`run_node` replays historical provider batches; `ForecastArchive` retains issued
forecasts; `evaluate` resolves outcomes later. Missing exact labels stay unavailable.

The [EMA notebook](../examples/ema_research.ipynb) demonstrates this path in QC
Research using `QuantBookServices`. It requires native LEAN services; memory does
not implement native indicators. Follow the
[EMA walkthrough](../examples/EMA_RESEARCH.md) and
[research reference](technical/RESEARCH.md) for public calls and metrics.

QuantBook replay admits split/dividend facts at effective boundaries and resets
affected resources before post-action reads. Historical receipt times are assumed
equal to observation ends. The EMA notebook has native kernel acceptance;
corporate-action replay, numerical parity and adjustment-vintage correctness
remain unverified. Predictive evaluation and
portfolio backtest returns answer different questions.

## 3. Compose a strategy

A graph can end in a predictive View. Trading requires an output declared as
exactly `PositionView`, with one actual terminal View at runtime:

```python
from sugarloaf.core.graph import Graph
from sugarloaf.core.types import InstrumentId
from sugarloaf.nodes.simple import PositiveSignalPositions
from sugarloaf.nodes.technical import EMASpread

asset = InstrumentId("USA:SPY")
graph = Graph().add_node("signal", EMASpread(asset))
graph.add_node("allocation", PositiveSignalPositions((asset,), budget=0.8))
graph.connect("signal", "views", "allocation", "views")
compiled = graph.set_output("allocation", "positions").compile()
```

Compilation verifies wiring. Evaluating this signal requires a provider with native
EMA support. Allocation requests 80% NAV for a positive score and zero otherwise;
it does not calibrate the score or optimize costs.
[Built-in nodes](../sugarloaf/nodes/README.md) describe available rules.

Combine branches with `PositionNetter` and explicit budgets/scopes, then
`PortfolioConstraintNode`. Graph constraints and execution-safety constraints
are configured separately. [Portfolio](technical/PORTFOLIO.md) explains valuation,
attribution and explicit exits.

## 4. Deploy and inspect

A factory returns `sugarloaf.deployment.Deployment`: fresh graph factory, typed
strategy settings, ticker bindings, environment, execution policy and safety
constraints. A TOML manifest selects `module:function` and bundle files. The
[EMA factory](../deployments/quantconnect/config.py) and
[manifest](../deployments/quantconnect/ema.toml) are complete examples.

Inspect the public EMA bundle offline:

```powershell
uv run --cache-dir .uv-cache sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

This writes local evidence under `artifacts/ema/`; it creates no QC project or
backtest. To run on QC, use `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml`
in an authenticated LEAN environment. It uploads, compiles, waits for the backtest
and retrieves results and logs. A project must already be selected in the manifest
or saved state; otherwise run `sugarloaf create --adapter quantconnect --manifest deployments/quantconnect/ema.toml`
once. Every action requires `--manifest`.
[Deployment](technical/DEPLOYMENT.md) documents commands and evidence retention.
An optional [external strategy repository](EXTERNAL_STRATEGIES.md) uses the same
interfaces.

Inspect `SUGARLOAF_RUN`, `SUGARLOAF_RESULT`, completed backtest errors and saved
source identity. A compile proves a build, not successful execution or an economic
edge. [Validation](VALIDATION.md) separates implementation from platform evidence.

## 5. Understand activation and order flow

Distinguish observation end, receipt/availability and the monotonic graph clock.
Late observations cannot rewind the graph. Triggered nodes and consumers of
changed/expired outputs run in dependency order; others retain outputs until expiry
or invalidation. Schedules are checked on arriving data, with no independent timer.

QC consolidators queue observations. `QuantConnectHost.on_data` creates an
`EventBatch`; `TradingSession.process` runs the graph and accepts its position
target. Execution reads fresh holdings and pending orders, submits the remaining
delta and applies cumulative fill feedback. Withdrawal cancels intent authority
but does not liquidate filled holdings.

Follow [runtime](technical/RUNTIME.md), [adapters](technical/ADAPTERS.md) and
[execution](technical/EXECUTION.md) for exact timing and failure behavior.

## 6. Change execution behavior

| Desired change | Owning component |
| --- | --- |
| Signal or allocation | Node and graph |
| Intent lifetime, target age, minimum trade value | `ExecutionPolicy` |
| Account exposure limits | Graph constraint node and deployment constraints |
| Additional order safety | Custom `RiskGate`, composed explicitly into the engine |
| Reconciliation or callback sequencing | Execution engine/session and host |
| Platform order types | Gateway capabilities and submission implementation |

The standard QC host constructs its engine explicitly; configuration has no
custom-engine registry. New order types require gateway implementation.
Preserve partial fills, cancellation confirmation and uncertain-submit reservations.

[Maintenance](MAINTENANCE.md) explains tests and diagnostics.
