# Positions and execution

Source: [position nodes](../../sugarloaf/nodes/positions.py),
[constraints](../../sugarloaf/portfolio/constraints.py),
[contracts](../../sugarloaf/execution/contracts.py),
[engine](../../sugarloaf/execution/engine.py),
[session](../../sugarloaf/execution/session.py).

## Target construction

A trading graph ends in one explicit PositionView. Amounts use quantities or
fractions of account NAV. [Portfolio composition](PORTFOLIO.md) owns quantity
valuation, branch netting, attribution, explicit exits and graph constraints.
The engine separately protects order execution with fresh broker state.

`TradingSession(graph,services,execution,on_output=None)` requires a position output.
It ignores identical replay, invalidates before lifecycle processing, runs the graph
and accepts exactly one terminal PositionView. Missing/multiple terminal Views or
an exception invalidate execution. Withdrawal cancels intentions and holds filled
positions; only explicit zero is an exit instruction.

## Interfaces and state

| Interface | Semantics |
| --- | --- |
| `ExecutionPolicy` | Order type/session/time-in-force, intent lifetime, maximum target age, minimum trade value in account currency |
| `ExecutionGateway.capability(policy)` | Explicitly accept/reject platform order semantics |
| `context()` | Fresh account, instruments, valuation, costs and constraints |
| `submit(intent) -> order_id` | Translate one signed quantity request; reserve local identity first |
| `cancel(order_id)` | Request cancellation; raise on rejection/failure; acceptance is not terminal confirmation |
| `cancel_status(order_id) -> Capability \| None` | Latest request outcome: None = unprocessed/unknown, supported = accepted, unsupported = rejected; never terminal order state |
| `ExecutionEngine.accept(target)` | Replace target, cancel superseded intents, reconcile against fresh gateway state |
| `invalidate()` | Clear target and request cancellations |
| `on_order_update(update)` | Apply/queue signed cumulative feedback; does not initiate reconciliation |
| `reconcile()` | Re-read platform state and submit outstanding target deltas |

`ExecutionState` holds target, intent ledger, sequence and kill switch.
`IntentState` holds immutable OrderIntent, platform ID, status, cumulative filled
quantity and cancel-request flag. The ledger supplements broker state; it never
becomes authoritative holdings. Terminal statuses: filled, canceled, rejected,
expired. Nonterminal reservations include reserved, submitted, open and unknown.

```text
reserve identity -> submit -> bind platform ID -> open/partial -> terminal
                    exception -> unknown (keep exposure reserved)
cancel request -> still pending -> terminal confirmation
```

`OrderUpdate(order_id,intent_id,status,filled,time,message)` requires a finite
signed cumulative fill quantity, an aware timestamp, and a nonempty platform order
ID. Allowed statuses are `open`, `submitted`, `partial`, `pending_cancel`,
`filled`, `canceled`, `rejected`
and `expired`. Fill statuses require nonzero cumulative fills. The engine rejects
wrong-sign/excess fills, identity mismatches and a `filled` status without the
complete submitted quantity before mutating state. Valid regressed fill totals
are ignored. Terminal state cannot regress to a nonterminal update, while later
cumulative fills can still be recorded. A complete cumulative fill becomes
`filled` even if an older callback's status lags behind the platform ticket.
Empty intent IDs permit external order feedback, which is diagnosed.

Expiry, supersession and withdrawal use the same cancellation path. A failed
request resets the request flag, records `CANCEL_FAILED`, and propagates the error;
the nonterminal order still reserves exposure. A subsequent reconciliation retries
the cancellation. The gateway's `cancel_status` permits a retry after a later
confirmed request rejection (`CANCEL_REJECTED`). Accepted/unprocessed requests
retain their flag and exposure until terminal order feedback. A failure to read
request status keeps that reservation and propagates.

The QC gateway checks the returned `OrderTicket.get_most_recent_order_response()`
and polls it for outstanding cancellation requests. `is_error` rejects the request;
`is_success=False` without an error can mean still unprocessed. Request success
does not prove cancellation. This follows the checked
[QC ticket documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/order-management/order-tickets)
and LEAN source for
[CancelOrder](https://github.com/QuantConnect/Lean/blob/master/Common/Securities/SecurityTransactionManager.cs)
and [OrderTicket](https://github.com/QuantConnect/Lean/blob/master/Common/Orders/OrderTicket.cs).
Local injected-boundary tests verify these distinctions; this path has not been
run in a native QC kernel or cloud deployment.

## Reconciliation algorithm

1. Obtain fresh Context. No target/kill switch requests cancellation; an expired or
   over-age target is invalidated. Cancel expired/superseded intents, retry failed
   requests, and check outstanding request responses. Wait while any known
   nonterminal intent has cancellation requested.
2. Translate each named target into lot-rounded quantities (truncate toward zero).
   Sort by desired-minus-held quantity, then instrument ID: sells submit first.
3. For each instrument, obtain another fresh Context and recompute target quantity
   using current NAV/valuation. Pending quantity includes platform remaining orders
   and nonterminal local reservations absent from the platform snapshot, by order ID.
4. Delta = desired − held − pending. Skip zero, subthreshold trades except zero
   targets, and a target/instrument already rejected by the platform.
5. Create a unique intent with policy expiry; run RiskGate. A readiness rejection
   is diagnosed without submission. Reserve identity, submit, then bind platform ID.
   Submission exceptions retain `unknown` exposure and propagate; never blindly retry.

All gateway Contexts created for a reconciliation are closed when that pass
finishes, including early returns and exceptions. Risk gates must not retain
readable snapshot handles between passes.

Changing target identity requests cancellation of old intents. Reaccepting identical
content may reconcile again; changed content under the same active identity fails.
A cancellation request does not release exposure. Orders can fill in any order:
submitting a sell does not fund a buy until authoritative state reflects a fill.

Hosts serialize callbacks. During engine submission, feedback queues until the
returned order ID is bound. The QC host additionally drains **all** pending feedback
before reconciliation, repeating if new synchronous feedback arrives. Otherwise
filled shares could count once in holdings and again in the local pending ledger.

## RiskGate

Before each submission, check kill switch, target cutoff/expiry/age, supported
equity instrument, tradability, session, finite nonzero whole-lot quantity, positive
NAV and executable valuation.

Construct two weight vectors from authoritative holdings plus pending orders:
lower adds only possible sells; upper adds only possible buys. Include local intents
missing from platform state once. Add the proposed order to these bounds. Check
worst gross ∑max(|lowerᵢ|,|upperᵢ|), minimum/maximum net, cash reserve, shorting and
position bounds. This assumes no favorable fill ordering.

Existing breaches may be remediated: compare before/after violation magnitudes in
the same sorted instrument coordinates. Allow the order only if no magnitude
increases and no new breach appears. Turnover separately includes remaining target
distance plus cumulative fills for that target identity. Borrow permission is
required when a sell can leave a short position; covers are not blocked by a borrow
test. Finally request platform buying-power approval.

## Extension boundaries

Use policy for order settings and thresholds; use constraints for exposure limits.
Inject a custom `RiskGate` into `ExecutionEngine` for extra safety rules. Change the
engine/session composition for different reconciliation behavior, and implement
gateway capabilities for new platform order types. The default QC host constructs
its engine explicitly; there is no configuration-level custom-engine loader.

Current executable scope is US equities, regular-session day market orders. Native
holdings adjustments belong to the platform. Unknown submission outcomes require
authoritative resolution; restart/recovery and durable intent retention are not
implemented. Intent history is unbounded. Tests: [execution](../../tests/test_execution.py),
[QC callbacks](../../tests/test_adapter_quantconnect.py),
[graph integration](../../tests/test_execution.py).

For module signatures and docstrings, see the [API reference](../api/index.md).
