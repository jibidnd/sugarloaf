# Core: messages, nodes and graphs

Source: [primitives](../../sugarloaf/core/primitives.py), [Views](../../sugarloaf/core/views.py),
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
of intervals, so the forecast horizon is part of the target. A difference target
contains two component targets. `return_target(instrument, interval)` builds a
scalar return and `relative_return(left, right, interval)` the left-minus-right
difference of two aligned returns. Constructing a general Target does not imply
that a particular consumer supports it.

A predictive claim is `View(target, statement, metadata)`: what is forecast and
how the forecast is stated. The statement is one of five representations.

| Representation | States | Answers |
| --- | --- | --- |
| `Estimate(value, forecast_error_scale, property='mean', level=None)` | One property of the target: mean, variance or the quantile at `level` | That property only |
| `Probability(events, probabilities, exhaustive=False)` | Probabilities of named events, each lower < return <= upper | The stated events only |
| `Distribution` (abstract) | A full law with `units`, `support` and `dimension` | Whatever the subclass implements, plus what follows from it |
| `Ordering(groups, temperature)` | Highest-to-lowest tied groups of the target's instruments | The order |
| `Score(value, calibration_id)` | A scalar on a named calibration; not an implied return or probability | Nothing beyond itself |

`Probability` is a partial claim about events; `Distribution` is a complete law.
Every representation has `capabilities` and `supports(query)` and the queries
`mean()`, `variance()`, `quantile(p)` and `probability(lower, upper)`; a query the
statement cannot answer raises `CapabilityError`. A `Distribution` subclass
implements what it can (`cdf`, `quantile`, `pdf` or `logpdf`, `mean`,
`variance`) and the base class derives event probabilities from the CDF and
density from log density or the reverse. Moments are not inferred from a
quantile function, which does not show whether they exist. A subclass may not
use a query name as a field. A joint law sets
`dimension` to its number of coordinates and owns its dependence assumptions.
`QuantileDistribution(levels, values, lower_bound, tail_scale_low,
tail_scale_high)` is the concrete scalar law built from stated quantiles: linear
quantile function between them, exponential tails beyond, closed-form moments.

`View` validates the pair: estimates and probabilities need a scalar or relative
return target; a distribution's units match the target and its dimension equals
the number of joint instruments; an ordering's instruments equal the target's
subject. Named subclasses fix one combination and remain for existing code:
`ReturnView` (scalar return + mean Estimate), `RelativeReturnView` (relative
return + mean Estimate), `ProbabilityView`, `DistributionView`, `RankingView` and
`ScoreView`. `PositionView` is not a View: it is an instrument→amount map,
`weight` or `quantity`, branch scope, metadata and optional attribution.

Error scale is positive and describes forecast uncertainty, not asset volatility.
Exhaustive probabilities must partition the real line and sum to one; other
events remain separate. Orderings contain each instrument once. Distributions
must behave immutably. Each consumer declares the representations it accepts and,
through `InputSpec.needs`, the queries it will ask, such as `("mean",)`.

## Node and graph interfaces

`Node` declares `inputs`, `outputs`, `requirements`, `triggers`, and
`evaluate(NodeInputs, Context) -> NodeOutputs`.

| Object | Contract |
| --- | --- |
| `InputSpec` | Accepted message classes; min/max count; optional statement classes, quantities and properties; `needs` queries every statement must answer |
| `OutputSpec` | Emitted message classes and optional semantic guarantees |
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
