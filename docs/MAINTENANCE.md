# Maintenance

Start with the behavior you need to change, the owning component and its existing
tests. Read [architecture](ARCHITECTURE.md) for the call chain, then the focused
reference and [API docstrings](api/index.md). Preserve unrelated changes.

## Choose the boundary

| Change | Reference | Useful tests |
| --- | --- | --- |
| View semantics and graph wiring | [Core](technical/CORE.md) | `test_contracts.py`, graph cases in `tests/` |
| Data/receipt timing and activation | [Runtime](technical/RUNTIME.md) | `test_data_clock.py`, `test_runtime.py` |
| Services, indicators and QC callbacks | [Market](technical/MARKET.md), [adapters](technical/ADAPTERS.md) | `test_adapter_quantconnect.py` |
| Allocation and exposure arithmetic | [Portfolio](technical/PORTFOLIO.md) | `test_simple_nodes.py`, `test_execution.py` |
| Orders, fills and cancellation | [Execution](technical/EXECUTION.md) | `test_execution.py` |
| Replay and outcome labels | [Research](technical/RESEARCH.md) | `test_research_runner.py`, `test_research_evaluation.py` |
| Packaging and evidence | [Deployment](technical/DEPLOYMENT.md) | Deployment tests in `tests/` |

Start with [contract regressions](../tests/test_contracts.py) and the relevant
module tests above. Use a regression
that distinguishes the intended behavior from the failure, rather than duplicating
implementation internals.

## Verify a change

From the repository root:

```powershell
uv run --cache-dir .uv-cache pytest
uv run --cache-dir .uv-cache ruff check .
```

Use controlled late/equal-time arrivals and expiry boundaries for runtime changes;
numerical cases with defined units for arithmetic; partial-fill/cancel/unknown
submit sequences for execution. Memory fixtures should state all prices, calendar
intervals, fills and confirmations. They cannot establish native market behavior.

Native QC changes require appropriate platform evidence in addition to local
injected-boundary tests: Python.NET overloads and indicator behavior need a native
kernel or backtest. Record actual source/configuration, completed errors and
relevant outputs. A compile alone establishes only build/import success.
[Validation](VALIDATION.md) records what has been verified.

## Diagnose missing output

Check the chain in order:

1. Did the provider admit the relevant observation at this receipt boundary?
2. Did a trigger, changed input, resource update or expiry activate the node?
3. Are declared histories, indicator windows and upstream input counts ready?
4. Did `evaluate` return a complete valid snapshot with correct source and expiry?
5. Did downstream allocation produce one terminal position View?
6. Did execution accept the target and pass session, valuation and risk checks?

Inspect `runtime.batch`, `runtime.store`, `runtime.diagnostics` and
`diagnostic_count`. Diagnostic retention is bounded; the count includes discarded
older entries. `NotReady` and `NumericalError` withdraw output and record a
diagnostic. Unexpected failures propagate after withdrawing affected outputs;
batch processing does not roll back all provider/node mutations.

## Diagnose orders

Inspect broker holdings/open orders alongside the engine's target and intent
ledger. Use signed cumulative fill quantities, not a sum of repeated callback
deltas. A cancel request retains pending exposure until terminal confirmation.
A failed or rejected cancellation remains visible and can be retried.

An exception during submission leaves an unknown local reservation. Resolve the
actual broker outcome before any resubmission. The ledger supplements broker
state; it never replaces authoritative holdings.

For QC, inspect `SUGARLOAF_RUN`, `SUGARLOAF_RESULT`, completed errors, ticket
feedback and the saved deployment snapshot. Keep source identity tied to the
specific run instead of substituting the current working tree.

## Maintain documentation

- Docstrings describe a class/function contract: parameters, units, returns,
  exceptions and concise usage where it helps.
- Technical references describe interactions, timing and consequential assumptions.
- Package READMEs introduce entry points and link to the detailed references.
- The root README showcases capabilities; the guide gives the shortest useful path.

Public docstrings use Google-style Args, Returns, Raises and Attributes sections
where relevant. Type annotations supply types; describe meaning, units and
consequential side effects without repeating signatures. Document expected
contract/capability failures without enumerating every internal exception.

Build or preview the documentation with the approved documentation dependency group:

```powershell
uv sync --group docs --cache-dir .uv-cache
uv run --group docs --cache-dir .uv-cache mkdocs serve
uv run --group docs --cache-dir .uv-cache mkdocs build --strict
```

Generated API pages read source/docstrings; architecture guides explain cross-module
behavior. Edit the source docstrings rather than hand-maintaining API signatures.
The build parses source without importing LEAN. `tools/docs.py` exposes existing
repository pages and selected source files at their repository-relative paths so
links work in both GitHub and the site; it creates no maintained copies. Mermaid
diagrams use a public JavaScript CDN, which requires network access for its build
availability check and browser rendering. CI builds the site strictly without
publishing it.
Keep each full contract in one place and cross-link it. Update validation claims
only when supporting evidence changes. Documentation edits need link/consistency
checks and executable checks for runnable examples.

## Current operational limits

There is no live restart/recovery, durable runtime/intent checkpoint or transaction
rollback after arbitrary failures. View identity, forecast archive and intent
retention are unbounded within a run. Keep credentials and generated data outside
source; ignored evidence requires backup. These constraints affect long-running
operation and are not fixed by reconnecting a host.
