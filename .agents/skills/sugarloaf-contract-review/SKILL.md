---
name: sugarloaf-contract-review
description: Review Sugarloaf framework contracts and consequential changes to interfaces, timing, lifecycle, research semantics, or execution. Use for requested reviews or changes needing cross-component reasoning; exclude routine local fixes and trading-edge evaluation.
---

# Sugarloaf Contract Review

Establish whether the requested behavior is coherent across the affected framework
boundaries. Keep review proportional to the change and distinguish current
behavior, accepted design, and a proposal. A review request alone does not authorize
implementation or cloud execution.

## Establish the contract

Read the affected source, callers, tests, and only the relevant specification:

- [Core](../../../docs/technical/CORE.md) and [runtime](../../../docs/technical/RUNTIME.md):
  declarations, slices, the node protocol and context, View identity and expiry, graph
  composition, `GraphRunner` dispatch, warm-up and the terminal target check.
- [Execution](../../../docs/technical/EXECUTION.md) and [portfolio](../../../docs/technical/PORTFOLIO.md):
  target meaning, constraint checks, `ExecutionPolicy` and the `order_quantity` sizing rule,
  order feedback.
- [Adapters](../../../docs/technical/ADAPTERS.md): the adapter interface, the QuantConnect
  mapping table, callback forwarding, universe membership and the memory adapter.
- [Research](../../../docs/technical/RESEARCH.md): forecast runs, the archive and its JSON
  form, outcome anchors, scoring, coverage and inference limits.
- [External packages](../../../docs/EXTERNAL_STRATEGIES.md): fresh graph factories,
  explicit dependencies, and source/configuration identity.

Use [validation](../../../docs/VALIDATION.md) to separate supported behavior from
unverified integration. Identify who owns each decision and which callers rely on
it. When docs and code disagree, report the discrepancy rather than silently
choosing a new contract. Vault sketches can explain intent but do not establish
implemented capabilities.

## Trace concrete behavior

Trace one realistic platform callback through the adapter (`Arrival`), `GraphRunner`
(per-node slice, callbacks in topological order, view carriage), the terminal target check
and `adapter.execute`, or through the forecast archive and evaluation. Inspect only the
failure cases that can change the conclusion:

- Which declarations a node makes, what its slice contains at that moment (universe
  membership, consolidated bars completing), indicator and window readiness, warm-up,
  expiry, and lifecycle records.
- Immutable view identity, duplicate or revised evidence, port specs, target units,
  horizons, score calibration, and fresh per-run node state.
- Withdrawal versus an explicit exit, overlapping position branches, holdings versus open
  orders counted as pending, superseded orders, platform refusals, order feedback.
- Adapter ownership of platform objects, unsupported capabilities, and private-package
  isolation in a selected deployment bundle.

Prefer explicit, readable behavior and the simplest design that satisfies the
contract. Avoid adding an abstraction solely to accommodate hypothetical reuse.
For a consequential platform assumption, check official QC/LEAN docs and the
relevant source version; cite checked evidence and distinguish inference.

## Verify and report

Look for meaningful existing tests first. Where verification is needed, choose a
small numerical, timing, lifecycle, or order sequence that discriminates correct
behavior from the plausible failure. Run the applicable checks when permitted;
do not submit cloud runs merely to complete a review. Native compatibility that
local fakes cannot establish remains unverified until platform validation.

Lead with actionable findings, each tied to a concrete trigger, consequence, and
source location. Separate defects from design alternatives and evidence gaps. If
no actionable defects are found, say so and state the material verification limits.
For an authorized implementation, retain the agreed scope, update the contract
docs and regression coverage where needed, and follow root verification rules.
