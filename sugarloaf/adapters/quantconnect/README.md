# QuantConnect adapter

The QC adapter connects neutral node/runtime contracts to LEAN subscriptions,
consolidators, indicators, calendars, account state and orders. Native imports and
Python.NET calls remain at this boundary; strategy nodes read Context services.
QC hosts callbacks, native data, clocks, indicators and order APIs. Sugarloaf
retains graph decisions, trading sessions and execution reconciliation.

## Module map

| Module | Entry points and responsibility |
| --- | --- |
| [services.py](services.py) | `QuantConnectDataServices` and `QuantConnectServices`: subscriptions, admission, resources and account reads |
| [host.py](host.py) | `QuantConnectHost`: callback serialization; `QuantConnectGateway`: fresh broker reads, order submission/cancellation |
| [lean.py](lean.py) | `LeanAPI`: delayed imports, native calls and injected test boundary |
| [research.py](research.py) | `QuantBookServices`: market-only historical replay and exact outcome anchors |
| [launcher.py](launcher.py) | Apply Deployment settings; start/finish diagnostic reports |
| [main.py](main.py) | Generic QCAlgorithm entry point forwarding callbacks |
| [cloud/packaging.py](cloud/packaging.py) | Explicit manifests and offline source bundles |
| [cloud/backtests.py](cloud/backtests.py) | Local QC project operations and backtest orchestration |
| [cloud/notebooks.py](cloud/notebooks.py) | Remote research-kernel execution and notebook outputs |

## Callback path

Consolidators queue completed inputs. `on_data` combines queued facts and lifecycle
events, with observation ends establishing cutoff and algorithm UTC time establishing
receipt. Warmup admits provider data without node decisions.
`TradingSession.process` evaluates the graph and accepts the final position.

Order callbacks queue even when synchronous. The host reads cumulative ticket fills,
drains all feedback and then reconciles; otherwise a fill can be counted in both
holdings and pending reservations. LEAN adjusts holdings for corporate actions;
the provider resets affected data/indicator resources and the runtime invalidates
old View authority.

See [architecture traces](../../../docs/ARCHITECTURE.md),
[full adapter contract](../../../docs/technical/ADAPTERS.md) and
[execution sequencing](../../../docs/technical/EXECUTION.md#reconciliation-algorithm).

## Research and deployment

`QuantBookServices` supports explicit instruments, bars and native indicators
without portfolio/execution services. Replay admits split/dividend facts at effective
boundaries; historical receipt equals observation end by assumption. Exact missing
labels stay unavailable. See [predictive research](../../../docs/technical/RESEARCH.md)
and [the EMA notebook walkthrough](../../../examples/EMA_RESEARCH.md).

Deployment uses a factory/manifest, not edits to the generic entry point.
The public command is `sugarloaf run --adapter quantconnect --manifest PATH`; Python scripts call
`sugarloaf.adapters.quantconnect.cloud.backtests.run_backtest`. Shared configuration/evidence/reporting
live in `sugarloaf.deployment`, while cloud operations stay in this QC adapter.
An existing project is required for a run; use explicit `create` once if needed.
Start with [the guide](../../../docs/GUIDE.md#4-deploy-and-inspect) and
[deployment reference](../../../docs/technical/DEPLOYMENT.md).

## Supported scope and evidence

Trading currently supports US equities and regular-session day market orders.
Native indicators include SMA, EMA, RSI and Donchian; their initialization/defaults
are provider assumptions, with no memory-formula fallback or parity promise.

[Injected-boundary tests](../../../tests/test_adapter_quantconnect.py) verify local
callback/resource behavior. They cannot establish Python.NET overload compatibility.
The EMA backtest and QuantBook notebook have native acceptance. Native numerical
parity, corporate-action replay and point-in-time adjustment-vintage correctness
remain unverified. Recorded cloud evidence and
remaining checks belong in [validation](../../../docs/VALIDATION.md).
