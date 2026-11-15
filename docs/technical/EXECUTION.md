# Execution

A trading graph's terminal node emits one `PositionView`. The runner checks it against the
portfolio constraints and passes it to `adapter.execute(target, policy)`, which sizes and
sends orders through the platform. Holdings, open orders, fills, partial fills and
cancellations are the platform's: the adapter reads them when it sizes a target and
forwards order feedback to the runner.

Source: [execution contracts](../../sugarloaf/execution/contracts.py),
[runner terminal check](../../sugarloaf/runtime/runner.py) (`GraphRunner._terminal`),
[QuantConnect `execute`](../../sugarloaf/adapters/quantconnect/adapter.py) and
[memory `execute`](../../sugarloaf/adapters/memory/adapter.py).
Tests: [memory adapter](../../tests/test_memory_adapter.py) (sizing, cancellation),
[QuantConnect adapter](../../tests/test_quantconnect_adapter.py) (tagged orders, refusals,
feedback), [runner](../../tests/test_runner.py) (rejected targets).

## Path of a target

1. The terminal port holds exactly one unexpired `PositionView` that differs from
   `runner.last_target`. Anything else sends nothing.
2. The runner merges the target over current holdings and checks it with
   `violations(...)` against `GraphRunner.constraints` (gross, net, per-position bounds,
   turnover). A violation records a `TARGET_REJECTED` diagnostic and sends nothing.
3. `adapter.execute(target, policy)` handles each instrument of the target, in identifier
   order:
   - no mark (or a non-positive one): an `OrderRecord` with status `skipped`;
   - with `cancel_on_new_target`, open orders of earlier targets in the instrument are
     cancelled first;
   - the order quantity is computed by `desired_quantity` and `order_quantity`;
   - a nonzero quantity is sent as the policy's order type, tagged with the target's
     `view_id`; a platform refusal is recorded as `rejected` with the platform's reason.
4. Order feedback arrives through the platform's callback as `OrderUpdate` records in
   `runner.updates`.

The runner sends a target once. A restated identical target, a withdrawn target and an
expired target send nothing; none of them liquidates holdings. An exit is an explicit zero.

## ExecutionPolicy

| Field | Default | Meaning |
| --- | --- | --- |
| `order_type` | `"market"` | `market`, `market_on_open` or `market_on_close`; the platform decides when such an order may be sent and how it fills |
| `time_in_force` | `"gtc"` | `gtc` or `day` |
| `min_trade_value` | `0.0` | Smallest order worth sending, in account currency |
| `rebalance_band` | `0.0025` | Smallest gap, as a fraction of NAV, worth an order for a weight target |
| `cancel_on_new_target` | `True` | Cancel open orders of earlier targets in an instrument before sizing it |

A weight target's desired quantity moves with every mark; the band keeps the runner from
re-trading that drift. An exit to zero is never suppressed by a threshold.

## Sizing

`desired_quantity(amount, units, price, nav, lot_size=1.0)` returns the desired quantity in
whole lots and exactly. A weight `w` wants `w × NAV / price` units; a quantity target wants
its amount.

`order_quantity(whole, exact, held, pending, price, nav, units, policy)` returns the signed
quantity to send now, or zero:

- the residual is `whole − held − pending`, where `pending` is the unfilled quantity of the
  platform's open orders still counted against the target;
- an exit to zero (`whole == exact == 0`) is always sent;
- an order worth less than `min_trade_value` is not sent;
- for a weight target, an order is not sent when the exact gap `|exact − held − pending|`
  is worth less than `rebalance_band × NAV`, or when the whole-lot order would leave the
  account no closer to the exact target.

Example: NAV 10,010, price 100.50, target weight 0.2. The exact quantity is 19.92 and the
whole quantity 19. Holding 20 shares, the gap is 0.08 shares (8.04, below the 25.03 band),
so no order is sent. Holding nothing, the order is 19 shares.

## Account records

| Record | Fields |
| --- | --- |
| `Portfolio` | `time`, `nav`, `cash`, `holdings` (signed quantities), `prices` (marks), `open_orders`, `currency="USD"`; methods `weight(instrument)`, `weights()`, `pending(instrument)` |
| `OpenOrder` | `order_id`, `instrument`, `quantity`, `filled` (signed cumulative), `tag`; property `remaining` |
| `OrderRecord` | `target_id`, `instrument`, `quantity`, `order_id`, `status` (`submitted`, `filled`, `skipped`, `rejected`), `reason` |
| `OrderUpdate` | `order_id`, `instrument`, `status`, `filled` (signed cumulative, never a delta), `time`, `tag`, `message` |

`OrderUpdate.status` is one of `new`, `submitted`, `partial`, `filled`, `pending_cancel`,
`canceled`, `rejected`, `expired`, `invalid`. The QuantConnect adapter maps LEAN's
`OrderStatus` names to these and raises `ContractError` on any other.

## Platform behaviour

On QuantConnect the order is `market_order`, `market_on_open_order` or
`market_on_close_order` with `asynchronous=True`, the target id as `tag` and an
`OrderProperties` carrying the time in force. LEAN holds a market order sent while the
exchange is closed for the next open, and refuses a market-on-close order too close to the
close; the adapter records what LEAN returns. On daily bars a `day` order sent at the close
expires before it can fill, which is why `gtc` is the default. The
[validation record](../VALIDATION.md) holds the native observations behind these statements
and their dates.

The memory adapter fills each order at the current mark when built with `fills=True`, and
otherwise keeps it open until `MemoryAdapter.fill(order_id)`.

## Two constraint checks

`GraphRunner(constraints=...)`, set from `Deployment.constraints`, checks every terminal
target. `PortfolioConstraintNode` inside a graph applies limits to a branch or to the final
target and withdraws it (a `NotReady` diagnostic) before it reaches the runner. Configure
each deliberately; see [portfolio](PORTFOLIO.md).
