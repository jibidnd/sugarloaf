# API reference

These pages are generated from the Python source and Google-style docstrings.
Use the [component guides](../ARCHITECTURE.md) to understand interactions and this
reference for signatures, parameters, return values and failure contracts.

| Component | Reference |
| --- | --- |
| Views, graph wiring, events and context | [Core](core.md) |
| Requirements, schedules and instrument selection | [Market data](market.md) |
| Clock, activation and View storage | [Runtime](runtime.md) |
| Built-in nodes and allocation constraints | [Nodes and portfolio](portfolio.md) |
| Broker snapshots, intents and reconciliation | [Execution](execution.md) |
| Provider services, memory fixtures, QC boundary and cloud tools | [Adapters](adapters.md) |
| Replay, issued forecasts and outcome evaluation | [Research](research.md) |
| Shared configuration, adapter dispatch, reporting and evidence | [Deployment](deployment.md) |

The build reads source statically; it does not import LEAN or run strategies.
Generated signatures and source listings track the checked-out version. Native
platform behavior still needs the evidence described in [validation](../VALIDATION.md).
