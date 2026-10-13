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
It ignores identical replay, runs the graph and accepts exactly one terminal
PositionView. Missing/multiple terminal Views or an exception invalidate
execution. A corporate action therefore cancels orders only when the runtime
withdrew the terminal target because of it; a target that does not involve the
affected instrument carries on. Withdrawal cancels intentions and holds filled
positions; only explicit zero is an exit instruction.

## Interfaces and state

| Interface | Semantics |
| --- | --- |
| `ExecutionPolicy` | Order type/session/time-in-force, intent lifetime, maximum target age, minimum trade value in account currency, rebalance band as a NAV fraction |
| `ExecutionGateway.capability(policy)` | Explicitly accept/reject platform order semantics |
| `context()` | Fresh account, instruments, valuation, costs and constraints |
| `submit(intent) -> order_id` | Translate one signed quantity request; reserve local identity first |
| `cancel(order_id)` | Request cancellation; raise on rejection/failure; acceptance is not terminal confirmation |
| `cancel_status(order_id) -> Capability \| None` | Latest request outcome: None = unprocessed/unknown, supported = accepted, unsupported = rejected; never terminal order state |
| `ExecutionEngine.accept(target)` | Replace target, cancel superseded intents, reconcile against fresh gateway state |
| `invalidate()` | Clear target and request cancellations |
| `on_order_update(update)` | Apply/queue signed cumulative feedback; does not initiate reconciliation |
| `reconcile()` | Re-read platform state and submit outstanding target deltas |

`ExecutionState` holds the target, the intent ledger and the intent sequence.
The ledger keeps every intent of the current target plus older intents that are
not yet terminal; finished intents of superseded targets are dropped, so it
does not grow with the length of the run.
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

1. With no target, request cancellation of outstanding orders and stop; no
   broker snapshot is taken. Otherwise obtain a fresh Context; an expired or
   over-age target is invalidated. Cancel expired/superseded intents, retry failed
   requests, and check outstanding request responses. Wait while any known
   nonterminal intent has cancellation requested.
2. Translate each named target into lot-rounded quantities (truncate toward zero).
   An instrument without a usable mark is skipped with a `VALUATION_UNAVAILABLE`
   diagnostic; the others still trade. Sort by desired-minus-held quantity, then
   instrument ID: sells submit first.
3. For each instrument, obtain another fresh Context and recompute target quantity
   using current NAV/valuation. Pending quantity includes platform remaining orders
   and nonterminal local reservations absent from the platform snapshot, by order ID.
4. Delta = desired − held − pending. Skip zero, a target/instrument already
   rejected by the platform, and orders below a threshold: `min_trade_value` in
   account currency, or for a weight target `rebalance_band` × NAV. Neither
   threshold suppresses an exit to zero.
5. Create a unique intent with policy expiry; run RiskGate. A readiness rejection
   is diagnosed without submission. Reserve identity, submit, then bind platform ID.
   Submission exceptions retain `unknown` exposure and propagate; never blindly retry.

All gateway Contexts created for a reconciliation are closed when that pass
finishes, including early returns and exceptions. Risk gates must not retain
readable snapshot handles between passes.

**Rebalance band.** A weight target's desired quantity moves with every mark.
Holding 20 shares against a 20% target, a 0.5% price rise makes the exact desired
quantity 19.92: the account is 0.08 shares off target, yet whole lots would say
"sell one". The band compares that exact drift, in account currency, with
`rebalance_band` × NAV (default 0.25%) and trades only beyond it. It also never
sends a whole-lot order that would leave the account no closer to the exact target:
held 50 with 49.55 wanted is 0.45 off, and selling one share would leave it 0.55
off the other way. In an account where one lot is worth more than the band,
single-lot corrections still occur whenever the exact target crosses a whole lot;
raise `min_trade_value` or the band to suppress them. Quantity targets are exact
instructions and are not banded.

Changing target identity requests cancellation of old intents. Reaccepting identical
content may reconcile again; changed content under the same active identity fails.
A cancellation request does not release exposure. Orders can fill in any order:
submitting a sell does not fund a buy until authoritative state reflects a fill.

Hosts serialize callbacks. During engine submission, feedback queues until the
returned order ID is bound. The QC host additionally drains **all** pending feedback
before reconciliation, repeating if new synchronous feedback arrives. Otherwise
filled shares could count once in holdings and again in the local pending ledger.

## RiskGate

Before each submission, check target cutoff/expiry/age, supported
equity instrument, tradability, finite nonzero whole-lot quantity, positive
NAV and executable valuation. Whether the exchange is open is not checked: see
[order types and timing](#order-types-and-timing).

Construct two weight vectors from authoritative holdings plus pending orders:
lower adds only possible sells; upper adds only possible buys. Include local intents
missing from platform state once. Add the proposed order to these bounds. Check
worst gross ∑max(|lowerᵢ|,|upperᵢ|), minimum/maximum net and per-position
bounds. This assumes no favorable fill ordering. The arithmetic is
`portfolio.constraints.breaches`, the same function graph nodes use.

Existing breaches may be remediated: compare each limit's violation size before
and after adding the order. Allow the order only if no size increases, which also
rules out a new breach. Turnover separately includes remaining target distance
plus cumulative fills for that target identity; an instrument that cannot be
valued is left out of that sum until it can trade. Borrow permission is
required when a sell can leave a short position; covers are not blocked by a borrow
test. Finally request platform buying-power approval.

## Order types and timing

`ExecutionPolicy.order_type` is `market`, `market_on_open` or `market_on_close`.
The policy says what to ask for. The platform decides when such an order may be
sent and how it fills, and those rules are a property of the backtest or brokerage
configuration, not of a node or of the engine.

- **No session check.** `RiskGate` does not refuse an order because the exchange
  is closed. A decision on a daily bar arrives at the close; the order is sent and
  the platform applies its rule.
- **Observed on QuantConnect** (LEAN 2.5.0.18178, default brokerage and fill
  models; [validation](../VALIDATION.md)): a market order sent while the exchange
  is closed becomes a market-on-open order and fills at the next opening price. A
  market-on-close order is refused inside the last quarter-hour of the session. A
  market-on-open order sent during the session on a minute feed is refused.
- **A refusal is final for that target.** When the platform declines to create an
  order the gateway raises `OrderRefused`. The engine records the intent as
  rejected with an `ORDER_REFUSED` diagnostic, reserves nothing, goes on to the
  target's other instruments, and does not ask again until a new target arrives.
  This differs from a submission whose outcome is unknown, which keeps its
  reservation and re-raises.
- **Time in force.** `time_in_force` is `gtc` (good till cancelled, the default)
  or `day`. The platform applies it. A `gtc` order does not outlive its target:
  when the target is replaced or expires the engine cancels whatever is unfilled.
  `day` adds the platform's own end-of-day expiry. On QuantConnect a day order
  sent at the close on a daily feed never fills: LEAN learns the next opening
  price only when the next daily bar arrives, at the close that ends the order's
  day, and it applies the expiry first. If the process stops, a `gtc` order stays
  at the broker until it is cancelled there; restart recovery is not implemented.
- **A split.** A weight target survives a split
  ([runtime](RUNTIME.md)). Its orders still waiting in the split instrument were
  counted in the old shares, so the session cancels them and the next
  reconciliation sizes them again from fresh holdings and prices. Orders in other
  instruments are untouched. A quantity target is withdrawn, and all its orders
  are cancelled.
- **No cancel timer by default.** `ExecutionPolicy.lifetime` is unset unless a
  strategy sets it. An order then stands until it fills, the platform ends it (a
  day order at the end of its day), or its target is replaced or expires. A
  lifetime is wall time and the engine applies it at every reconciliation,
  including one caused by a corporate-action batch at midnight. Seen natively:
  with a five-minute lifetime, a daily-bar strategy's orders, sent at the close
  and held by LEAN for the next open, were all cancelled at midnight on every
  night that some universe member had a corporate action
  ([validation](../VALIDATION.md)). Set a lifetime only to cancel sooner than the
  platform would, and make it longer than any wait the order type implies.

## Feedback and broker state

Holdings come from the broker at every read; the engine's own ledger says which
of its orders are still pending. The two must describe the same moment.

- A gateway passes order feedback to `on_order_update` as it arrives. An idle
  engine applies it at once. During a reconciliation it is queued until the order
  being submitted has its platform ID, and applied before broker state is read
  for the next instrument.
- The reason: sending an order can make the platform fill other, older orders
  before the call returns. Their fills are in the next holdings read; if their
  feedback were still queued they would also count as pending, and each would be
  traded against twice. Seen natively on daily bars, where all of yesterday's
  orders fill in one instant: a single drift order sent in that instant produced
  37 orders reversing fills, then 37 more undoing those
  ([validation](../VALIDATION.md)).

## Extension boundaries

Use policy for order settings and thresholds; use constraints for exposure limits.
Inject a custom `RiskGate` into `ExecutionEngine` for extra safety rules. Change the
engine/session composition for different reconciliation behavior, and implement
gateway capabilities for new platform order types. The default QC host constructs
its engine explicitly; there is no configuration-level custom-engine loader.

Current executable scope is US equities, regular-session day or
good-till-cancelled market, market-on-open and market-on-close orders. Native
holdings adjustments belong to the platform. Unknown submission outcomes require
authoritative resolution; restart/recovery and durable intent retention are not
implemented. `max_target_age` defaults to four days so a Friday-close decision can
trade on Monday; the target's own expiry is the precise control. Tests: [execution](../../tests/test_execution.py),
[QC callbacks](../../tests/test_adapter_quantconnect.py),
[graph integration](../../tests/test_execution.py).

For module signatures and docstrings, see the [API reference](../api/index.md).
