# API reference

These pages are generated from the Python source and its Google-style docstrings. The
[architecture](../ARCHITECTURE.md) and the component references explain how the modules
interact; these pages give signatures, parameters, return values and failures.

| Component | Modules | Page |
| --- | --- | --- |
| Declarations, records and slices, node protocol, views, graphs, universe rules, errors | `sugarloaf.core.*` | [Core](core.md) |
| Graph runner and node context | `sugarloaf.runtime.runner` | [Runtime](runtime.md) |
| Built-in nodes and constraint arithmetic | `sugarloaf.nodes.*`, `sugarloaf.portfolio.constraints` | [Nodes and portfolio](portfolio.md) |
| Execution policy, account records, sizing | `sugarloaf.execution.contracts` | [Execution](execution.md) |
| Adapter interface, memory and QuantConnect adapters, launcher, cloud tooling | `sugarloaf.adapters.*` | [Adapters](adapters.md) |
| Forecast archive, serialization, anchors, evaluation, inference | `sugarloaf.research.*` | [Research](research.md) |
| Deployment configuration, evidence, reporting, command dispatch | `sugarloaf.deployment.*`, `sugarloaf.cli` | [Deployment](deployment.md) |

The build reads source statically; it imports no LEAN code and runs no strategy. Native
platform behaviour needs the evidence described in [validation](../VALIDATION.md).
