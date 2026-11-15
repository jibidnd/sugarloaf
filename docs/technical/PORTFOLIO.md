# Portfolio composition and constraints

Position nodes turn forecasts into desired holdings and combine strategy branches. They
send no orders; the runner and the adapter do ([execution](EXECUTION.md)).

Source: [simple nodes](../../sugarloaf/nodes/simple.py),
[position nodes](../../sugarloaf/nodes/positions.py),
[constraint arithmetic](../../sugarloaf/portfolio/constraints.py).
Tests: [nodes](../../tests/test_nodes.py), [view axes](../../tests/test_view_axes.py),
[runner](../../tests/test_runner.py).

## Forecast aggregation and allocation

`MeanViewAggregator(lifetime=timedelta(days=4))` averages claims that share a claim family,
the exact `Target` and, for scores, the calibration id or, for probabilities, the event
definitions and exhaustiveness. A return mean keeps the largest input error scale, which
claims no independence or gained confidence. Distributions are refused. Outputs expire no
later than their parents and their target ends.

`PositiveSignalPositions(instruments, budget=0.9, scope="account", lifetime=timedelta(days=4))`
selects instruments with a positive mean, a positive score or `P(return > 0) > 0.5`, divides
`budget` equally among them as account-NAV weights and emits explicit zeros for the rest.
It needs one current claim per configured instrument, with one horizon and return
convention; a missing claim raises `NotReady`, which withdraws the target instead of
liquidating.

Both nodes declare no data (`inputs = ()`); they run when an upstream output changes.

## Position units and valuation

`PositionView.positions` maps instruments to amounts in `weight` or `quantity` units, with a
branch `scope` and optional `attribution`. Weights are fractions of account NAV, never of a
branch.

`position_weights(view, portfolio)` converts a target to weights: a weight target as is; a
quantity target as `quantity × mark / NAV`, raising `NotReady` when an instrument with a
nonzero quantity has no mark and `ContractError` when NAV is not positive.

A weight target needs maintenance trades as marks and NAV move; the execution
[rebalance band](EXECUTION.md#executionpolicy) suppresses those too small to matter. A
quantity target fixes units, not an account fraction.

## PositionNetter

`PositionNetter(budgets, lifetime=timedelta(days=4))` has one input port per key of
`budgets`, each accepting exactly one `PositionView`. Its `update`:

1. requires each branch view's `scope` to equal its port name;
2. converts quantity branches to weights at `ctx.portfolio` marks and checks each branch's
   gross weight against its budget;
3. refuses a branch that drops an instrument it held nonzero before (emit an explicit zero
   instead);
4. sums contributions per instrument, keeps the branch weights as `attribution` and the
   branch view ids as parents;
5. emits an account-scoped weight target expiring at the earlier of its lifetime and its
   parents' expiries.

Budgets are limits, not multipliers: branches asking for 20% and 5% of one instrument give
a 25% target whatever their budgets. A missing branch raises `NotReady`, so the netter emits
nothing until every branch has a view; [examples/memory.py](../../examples/memory.py) shows
this with two scheduled branches.

## PortfolioConstraintNode

`PortfolioConstraintNode(constraints)` merges the proposed weights over current holdings
(instruments the target omits stay held), checks them with `violations` and raises
`NotReady("TARGET_REJECTED: ...")` when a limit fails. It never rescales. A passing target
is re-emitted as a weight target with the node as source.

## PortfolioConstraints

All limits are fractions of account NAV, compared with tolerance `1e-8`:

| Field | Default | Limit |
| --- | --- | --- |
| `gross_limit` | 1.0 | `Σ|wᵢ| ≤ gross_limit` |
| `net_min`, `net_max` | 0.0, 1.0 | `net_min ≤ Σwᵢ ≤ net_max`; a lower `net_max` keeps a cash reserve |
| `min_weight`, `max_weight` | 0.0, 1.0 | Bounds on each nonzero position; a negative `min_weight` permits shorts |
| `turnover_limit` | 2.0 | `Σ|wᵢ − currentᵢ|` for one target |

Zero is always an allowed position. The defaults describe an unlevered long-only account.
`PortfolioConstraints(gross_limit=1.6, min_weight=-0.1)` permits a 130/30 book whose shorts
are each at most 10% of NAV.

`violations(weights, constraints, current=None)` returns the names of failed limits.
`breaches(low, high, constraints)` measures how far a range of weights lies outside each
limit, in NAV fractions. A USD 10,000 account holding USD 1,000 of an instrument the target
omits keeps that 10% in the check, so a 95% target elsewhere breaches a 100% gross limit.

The runner applies `Deployment.constraints` to every terminal target in addition to any
`PortfolioConstraintNode` in the graph.

## Authority and exits

Expiry, withdrawal, missing input and universe removal end a target's authority; none of
them sells holdings. An exit is an explicit zero.
