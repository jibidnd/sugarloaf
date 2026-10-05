# Portfolio composition and constraints

Position nodes turn forecasts into desired holdings and combine strategy branches.
They do not submit orders. [Execution](EXECUTION.md) separately reaches the target
subject to fresh broker state and order risk.

Source: [simple nodes](../../sugarloaf/nodes/simple.py),
[position nodes](../../sugarloaf/nodes/positions.py),
[portfolio arithmetic](../../sugarloaf/portfolio/constraints.py).

## Forecast aggregation and allocation

`MeanViewAggregator` combines only compatible claims of the same concrete View
type and exact Target. Scores also require identical calibration IDs; probabilities
require matching event definitions/exhaustiveness. Its retained error scale does
not imply independent evidence or newly calibrated confidence.

`PositiveSignalPositions` selects assets with positive return/score or explicit
P(return > 0) > 0.5, equally dividing an account-NAV budget. It emits zeros for
other configured assets. Missing evidence withdraws authority instead of forcing
a sale. These baseline rules do not convert rankings, distributions or relative
returns. See [built-in node rules](../../sugarloaf/nodes/README.md) for exact inputs.

## Position units and valuation

`PositionView.positions` maps stable instruments to desired amounts, with units
`weight` or `quantity`, explicit branch scope and optional attribution.
Weights are account-NAV fractions, never branch-NAV fractions.

`position_weights(view, ctx)` requires positive NAV. Quantity conversion uses:

```text
unit value = mark × contract/share multiplier × account-currency FX
weight = quantity × unit value / account NAV
```

An active weight target may require maintenance trades as NAV/marks move.
Quantity targets freeze desired units, not an account percentage. Missing valuation
is a readiness/capability failure; it is not a zero price.

## PositionNetter

`PositionNetter(budgets, lifetime)` creates one required input port per branch,
each accepting exactly one PositionView. Its `evaluate`:

1. Checks the View scope matches its port.
2. Converts each branch to account weights and checks its gross budget.
3. Rejects omission of a previously nonzero branch instrument; require explicit zero.
4. Sums instrument contributions and preserves branch attribution and parent IDs.
5. Emits an account-scoped View expiring at the earlier of its configured lifetime
   and all parent expiries.

Budgets are limits, not multipliers. If branch A requests 20% and B requests 5%
SPY, the target is 25% SPY; assigning budgets of 40% each does not scale either
request. Branch contributions can offset, but attribution preserves the individual
weights. A fill does not establish which branch owns those shares; strategies
needing branch holdings must supply an explicit attribution rule.

## PortfolioConstraintNode

`PortfolioConstraintNode(constraints).evaluate` merges proposed weights over
current holdings. Unspecified instruments remain held. It calls `violations` and
raises `NotReady("TARGET_REJECTED: ...")` when infeasible; it does not rescale.

Checks use tolerance 1e-8:

- Shorting permission and nonzero position minimum/maximum. Zero is always an exit.
- Gross exposure ∑|wᵢ| ≤ min(budget, gross_limit).
- net_min ≤ ∑wᵢ ≤ min(net_max, 1 − cash_reserve).
- Turnover ∑|wᵢ − currentᵢ| ≤ turnover_limit.

A USD10,000 account holding USD1,000 of an unspecified asset keeps that 10%
exposure in feasibility checks. A new 95% long target elsewhere can therefore
breach a 100% gross limit even if its own weight is below that limit.

Graph constraints express target feasibility. `Deployment.constraints` separately
configures execution safety for pending orders and possible fill orderings.
Configure both intentionally; the [RiskGate](EXECUTION.md#riskgate) covers the
execution layer.

## Authority and explicit exits

Expiry, withdrawal, missing input, universe removal and resource cleanup cancel
target authority; none means liquidation of filled holdings. Emit an explicit
zero for an exit. The netter remembers previously nonzero managed instruments to
catch accidental omission during branch replacement.

Readiness failures withdraw output and propagate; invalid branch scope/budget or
ambiguous managed-position omission raises `ContractError`.
[Baseline tests](../../tests/test_simple_nodes.py) cover alignment and allocation;
[execution tests](../../tests/test_execution.py) include netting and feasibility.
