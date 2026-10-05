# Core: messages, nodes and graphs

Source: [types](../../sugarloaf/core/types.py), [Views](../../sugarloaf/core/views.py),
[node interfaces](../../sugarloaf/core/node.py) and [graph compiler](../../sugarloaf/core/graph.py).
This reference owns message semantics and graph wiring. See [market](MARKET.md),
[runtime](RUNTIME.md), [adapters](ADAPTERS.md) and [execution](EXECUTION.md) for their
focused contracts; the [API reference](../api/index.md) lists public interfaces.

## Messages and units

All timestamps are aware; adapters normalize to UTC. `InstrumentId(value)` is a
stable identity independent of ticker. `Interval(start,end)` requires end > start;
`years` uses ACT/365 calendar time. Returns are decimal simple returns; position
weights are fractions of account NAV, not branch NAV.

`Metadata(view_id, source, cutoff, expires_at, parents=(), model_version='1')`
identifies immutable content. Source is the registered node ID; cutoff is logical
graph time. Expiry must exceed cutoff. Parents are unique and exclude self.
Runtime requires cutoff <= now < expiry. Receipt provenance is recorded separately
by the research archive; it never rewrites View metadata.

`Target(subject, quantity, time, units='decimal', convention='price_return',
components=())` specifies what a prediction concerns. Subjects may be scalar/joint
instruments or another explicit subject; time is an observation, interval or tuple
of intervals. A difference target contains two component targets. Constructing
a general Target does not imply that a particular consumer supports it.

| View | Payload and meaning |
| --- | --- |
| `ReturnView` | Scalar return target + `Estimate(value, forecast_error_scale, property, quantile)` |
| `RelativeReturnView` | Left-minus-right return target + estimate |
| `ProbabilityView` | Target + events/probabilities; each event is lower < return <= upper |
| `DistributionView` | Target + law with explicit support/units; law owns joint dependence |
| `RankingView` | Joint return target + highest-to-lowest tied groups and positive temperature |
| `ScoreView` | Target + scalar value and calibration identity; not an implied return/probability |
| `PositionView` | Instrument→amount map, `weight` or `quantity`, branch scope, metadata, optional attribution |

Estimate can represent mean, variance or quantile, but ReturnView and
RelativeReturnView currently accept **mean only**. Error scale is positive and
describes forecast uncertainty, not asset volatility. Exhaustive probabilities
must partition the real line and sum to one; other events remain separate.
Rankings contain each instrument once. Distribution laws must behave immutably.
View constructors validate their concrete semantics. Each consumer node declares
the View types and statement semantics it supports.

## Node and graph interfaces

`Node` declares `inputs`, `outputs`, `requirements`, `triggers`, and
`evaluate(NodeInputs, Context) -> NodeOutputs`.

| Object | Contract |
| --- | --- |
| `InputSpec` | Accepted View classes; min/max count; optional statement classes, quantities and properties |
| `OutputSpec` | Emitted View classes and optional semantic guarantees |
| `NodeInputs.by_port` | Immutable port→View tuples; incoming edges concatenate in registration order |
| `NodeOutputs.by_port` | Complete replacement of every output port; omission/empty tuple withdraws old Views |
| `NodeOutputs.resources` | None retains declarations; `ResourceUpdate(requirements,triggers)` replaces them next batch |

`Graph.add_node(id,node)`, `connect(src,port,dst,port)`, `set_output(id,port)` build
wiring; `compile()` freezes it. Compilation rejects duplicate/unknown identities,
unknown ports, incompatible classes/declared semantics, invalid input counts,
disconnected required inputs, cycles and a nonterminal designated output node.
Topological sorting emits alphabetically sorted ready groups. Required input
cardinality is checked again against actual Views at evaluation.

`CompiledGraph` holds nodes, edges, order and output. Node instances remain mutable
run state; do not share them across runs. Predictive terminal outputs are valid.
`require_positions()` restricts trading attachment to an output declared as exactly
`(PositionView,)`; the session checks actual output cardinality at runtime.

## Data declarations and Context

Requirements authorize service reads; triggers determine evaluation. Context is a
lazy, batch-bound read interface that must not be retained after processing.
The [market reference](MARKET.md) owns requirement types, units, readiness,
availability, exact anchors and snapshot caching.

## Clock, arrival and activation

Graph time advances on data arrivals and never rewinds. Receipt time separately
governs availability. Identical last-batch replay is a no-op; schedules run when
incoming data cross their boundary. See [runtime timing](RUNTIME.md#clock-arrival-and-activation).

## Processing algorithm

Runtime admission, activation, validation, complete output replacement, expiry,
lifecycle invalidation and failure propagation are documented in the
[runtime reference](RUNTIME.md#processing-algorithm). Existing node output is retained
until replacement, expiry or withdrawal; a missing target does not liquidate holdings.

[Contract tests](../../tests/test_contracts.py) cover Views, targets and wiring.
[Runtime tests](../../tests/test_runtime.py) cover output replacement/identity.

## Authoring flow

A data-only node has empty input ports and declares services/triggers. A consumer
declares the exact View families it accepts. In both cases:

1. Read only declared Context services and supplied input ports.
2. Calculate the claim with explicit target, horizon and units.
3. Construct Metadata with the registered node source and a new immutable ID.
4. Return NodeOutputs containing the complete current output snapshot.

The runtime validates port semantics and metadata before exposing the output.
An output View with a new ID represents a new claim, even if its numeric payload
matches a previous one. Reusing an ID requires exactly identical content.
See [the local minimal example](../GUIDE.md#1-implement-a-node) and
[built-in nodes](../../sugarloaf/nodes/README.md) for concrete implementations.
