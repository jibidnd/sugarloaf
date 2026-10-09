# Runtime, events and clocks

[GraphRuntime](../../sugarloaf/runtime/runtime.py) turns serialized arrivals into
selective graph evaluations. It owns active outputs and immutable View identities;
it does not submit orders. [TradingSession](EXECUTION.md#target-construction)
composes execution around it.

## Events and batches

`Event(kind, instrument=None, interval=None, key=None, payload=None, end=None)`
describes a fact with optional matching filters and observation end.
`EventBatch(sequence, cutoff, events=(), received_at=None)` supplies a nonnegative
sequence, data cutoff and receipt boundary; receipt defaults to cutoff.
Data ends, cutoff and lifecycle effective times cannot follow receipt.
Payloads use neutral immutable values, including `LifecycleEvent`.

An identical last arrival is ignored. Other nonincreasing sequences or regressed
receipt times fail with `ContractError`. Equal event-time arrivals can still be
distinct batches. Hosts must serialize processing; reentrant calls are rejected.

## Clock, arrival and activation

For each arrival:

```text
data boundary = latest event.end, or declared cutoff if none
graph time = max(previous graph time, data boundary)
```

A 10:01 observation received 10:03 advances the graph to 10:01 if it is the newest
data. A later-arriving 10:00 observation cannot rewind it. Provider reads still
check receipt/availability separately. There is no global barrier waiting for
unrelated instruments.

A `Trigger` matches its kind and any specified instrument, interval and key.
A scheduled trigger instead uses a wall time/timezone/calendar or session open/close
plus offset. Providers check occurrences crossed in `(previous tick, current tick]`.
Missed occurrences coalesce once. Startup checks the first tick's local day.
Replaced schedules check from the previous completed graph tick, not all history.

**Schedules require arriving data.** No independent timer creates graph ticks.
A callback may queue a scheduled fact, but the host still evaluates at a data
boundary. Expiry also takes effect when processing reaches its boundary.

## Processing algorithm

`GraphRuntime(graph, services, on_output=None).process(batch) -> ViewStore`:

1. Validate arrival identity/order and normalize the monotonic clock.
2. Install pending resource replacements; call `services.apply(batch)`; append
   the facts it returns (for example a changed universe).
3. Expire Views with expiry ≤ now. Activate their downstream consumers/descendants,
   resource-changed owners, matching triggers and changed-universe readers/owners.
4. On lifecycle facts, withdraw and re-activate only the affected nodes and their
   descendants, retire their View identities, and advance basis watermarks
   (see [ViewStore and lifecycle](#viewstore-and-lifecycle)).
5. Visit activated nodes once in topological order. Assemble input ports, create
   Context, validate input cardinality/semantics and readiness, then evaluate.
6. Validate output source, cutoff, expiry and port semantics; atomically replace
   the node's complete snapshot. Changed content activates direct consumers.
7. Call `on_output(node_id, outputs, batch)` for every successful emission, even
   unchanged content; queue any resource replacement for the next batch.
8. Record the original successful arrival for replay identity and normalized
   `runtime.batch`; close evaluation Contexts.

Within a batch, consumers see replacements from upstream nodes already visited.
Multiple activation causes do not produce multiple evaluations of the same node.
A failed batch is not recorded as completed, but processing does not roll back
arbitrary provider or node state.

## ViewStore and lifecycle

`ViewStore.get(node_id, port=None)` returns a node snapshot or port tuple.
`replace` validates identity before replacing the whole snapshot; `withdraw`
replaces it with empty output; `expire` removes expired Views and reports changed
owners. Omitted output ports are withdrawn, not retained.

The store keeps a ledger of the most recently issued identities (100,000 by
default). Reusing a remembered ID with different content, or duplicating an ID
within a snapshot, fails. The bound keeps a long minute-level run from retaining
every View it ever issued. Unactivated nodes retain active
outputs subject to expiry/invalidation. The ledger is not a forecast archive:
[ForecastArchive](RESEARCH.md#issued-forecasts) separately retains issued predictions.

A lifecycle event affects a node when either:

- the node declares bar, quote or indicator data for that instrument whose price
  basis the event changes. A split changes every basis. A dividend changes only
  `adjusted` and `total_return` history; raw and split-adjusted prices stay
  valid. A named-universe selection is assumed to contain the instrument;
- for any event other than a dividend, one of the node's active Views names the
  instrument (as a target subject or a position).

Affected nodes and all their descendants are withdrawn, their View IDs retired,
and they are re-evaluated in the same batch. Everything else is untouched: a
dividend on one stock of a 500-name universe no longer withdraws the other 499
forecasts or cancels every order.

A new View concerning an affected instrument must have an information cutoff at
or after the latest effective lifecycle boundary. After a non-dividend event that
holds for every node; after a dividend, for the affected nodes. Even a late
older action cannot lower that watermark.
The provider resets affected resources; broker holdings adjustments remain native.
The runtime neither transforms old Views nor liquidates holdings.

## Concrete trace

Suppose `signal → allocation` is the graph and the signal triggers on a completed
bar:

- Batch 1 admits the bar; signal emits a new score; allocation sees it and emits
  a position target.
- Batch 2 contains unrelated data. Neither node runs if no other activation occurs;
  their outputs remain active.
- Batch 3 crosses the score's expiry. The score is removed; allocation is activated.
  If its required input is now missing, its output is withdrawn.
- Execution loses target authority and requests cancellation. Filled holdings remain
  until a later explicit target changes them.
- A replay of batch 3 is a no-op.

## Failures and verification

| Failure | Result |
| --- | --- |
| `NotReady`, `NumericalError` during evaluation | Withdraw node output, record diagnostic, propagate effective input change |
| Other node exception | Withdraw node and descendants, propagate to host |
| `ContractError` | Invalid arrival, wiring/message or operation; do not reinterpret as a hold |
| `CapabilityError` | Unsupported service or operation |
| `SnapshotError` | Context no longer represents readable provider state |

Diagnostics use bounded retention; `diagnostic_count` counts all readiness/numerical
failures. The identity ledger is bounded. Durable restart is unsupported.

[Clock tests](../../tests/test_data_clock.py),
[runtime tests](../../tests/test_runtime.py) and
[contract tests](../../tests/test_contracts.py) cover late/equal-time data, replay,
schedules, expiry, lifecycle, replacement and invalid identities.
