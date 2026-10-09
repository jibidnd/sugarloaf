# Adapters

Adapters supply declared data, instruments, calendars and optional account services
to the graph. Nodes consume neutral contracts; LEAN objects stay inside the QC
boundary. See [market](MARKET.md), [runtime](RUNTIME.md), [execution](EXECUTION.md) and
[research](RESEARCH.md) for data, timing, reconciliation and archive contracts.

## Components and lifecycle

| Component | Responsibility |
| --- | --- |
| `ProviderServices` | Declaration guards, lazy Contexts, shared cache, readiness, selection and ownership |
| `MemoryServices` | Explicit market/account/calendar fixtures; no indicator formulas |
| `QuantConnectDataServices` | Native subscriptions, consolidation windows, indicators and universes |
| `QuantConnectServices` | QC data plus account, valuation, costs and trading capabilities |
| `QuantConnectHost` / `QuantConnectGateway` | Serial callback admission, TradingSession, order submission and feedback |
| `QuantBookServices` | Market-only historical replay using the QC data implementation |
| `LeanAPI` | Delayed platform imports and Python.NET calls; injectable test boundary |

The runtime uses these provider methods:

1. `install(node_id, ResourceUpdate)` replaces one node's complete requirements
   and triggers at a runtime boundary. Shared universe definitions must agree
   across owners.
2. `apply(EventBatch)` admits a boundary, refreshes membership/lifecycle facts and
   ownership, and invalidates older Contexts. It returns the facts it discovered
   (for example a changed universe) for the current runtime pass.
3. `context(batch=None, node_id="")` binds lazy facades to a provider version and
   batch. Empty node ID is reserved for execution account access.
4. `ready(node_id, context)` checks history, freshness, membership, instruments
   and indicators. A requirement declared with `require_all=False` is exempt
   from the per-instrument check. Recorded states are `requested`, `warming`, `unsupported`
   and `ready`.
5. `schedule_due(schedule, after, through)` tests occurrences in `(after, through]`.
   The runtime controls invocation and coalescing.

Subclasses supply data/indicator readers, instrument metadata, calendars
and optional account/cost/capability hooks. Contexts expose `market`, `instruments`
and `constraints`; account providers also expose `portfolio`, `costs` and
`valuation`. Context creation does not eagerly read the account or mirror the
platform data store.

## Admission, time and declarations

`Context.now` is logical data time; `Context.available_at` is receipt time. Bar,
quote and scalar reads require observation time ≤ `now` and availability time ≤
`available_at`. QC also requires native input admission. Provider mutation makes
existing Contexts fail with `SnapshotError`.

Compatible reads share a provider-version cache, but each node first passes its
own requirement and selection checks. Lookback and freshness are checked after
time filtering. Disallowed fill-forward bars are excluded. An undeclared
requirement or out-of-selection read raises `CapabilityError`.

`InstrumentRequirement(Selection())` permits metadata for input/account-dependent
instruments without subscribing to prices. Empty portfolio selection means the
whole account. Restricted selection filters holdings, prices and open orders;
NAV and cash remain account-wide.

`market.anchor` reads an exact timestamp from declared convention-compatible bar
history. Price-return anchors accept raw/split-adjusted bars; total-return anchors
accept total-return/adjusted bars. Missing anchors are not fetched or approximated.

## QC callback order

Deployment forwards `OnData`, `OnSecuritiesChanged` and `OnOrderEvent` to the host.
Consolidators queue completed native inputs and `bar_close` facts. External
schedules can queue named facts with `host.schedule`. Neither runs nodes directly.

`on_data` combines queued facts and lifecycle events. Actual bar/quote ends and
timestamped facts establish the data cutoff; algorithm UTC time is receipt time.
Empty callbacks do not advance the graph or sequence. Facts without new data use
the existing graph clock. Before the first observation, lifecycle feedback
invalidates execution immediately and waits for a data clock. Platform warmup
admits provider batches without node decisions.

`QuantConnectDataServices.apply` performs these operations in order:

1. Collect eligible retired universe sources.
2. Apply neutral membership/lifecycle changes, rebuild owners and invalidate
   previous Contexts.
3. Remove the affected instrument's consolidation/indicator state after a split,
   or after a dividend when that state uses dividend-adjusted prices.
4. Synchronize resources and warm new resources through the cutoff.
5. Admit queued observations and update matching native indicators.
6. Advance the provider version again after resource/data mutations, before node
   evaluation.

`TradingSession` processes the graph and terminal target. Order callbacks can
arrive synchronously during submission. While busy, the host queues them. It
drains **all** cumulative ticket updates before reconciling once, repeating if
reconciliation creates more feedback. Reconciling between queued fills would count
filled positions as both holdings and local outstanding quantities. Ticket fills
are cumulative; event deltas are not summed again.

## Resources, prefill and ownership

Backing data identity comprises requirement kind, stable instrument, interval,
session, normalization and fill-forward policy. Compatible owners share one
consolidator and one bounded window sized to the largest lookback. Each native
bar is converted to a neutral `Bar`/`Quote` once, when it is admitted, and the
window stores those. Freshness is per reader. Enlarging a window rebuilds and
warms it.

Neutral IDs map to LEAN security IDs; ticker changes preserve identity.
`register(instrument, ticker)` supplies an initial mapping. Metadata rejects
non-equity/non-USA securities. Minute equity subscriptions feed requested
whole-minute consolidations up to one day. Concurrent requests for one security
must agree on session, normalization and fill-forward semantics. Schedule calendar
instruments own implicit minute feeds without granting nodes price-read permission.

Prefill uses typed history ending at an explicit cutoff. Regular daily requests
use daily history; intraday and extended-session daily requests use minute history
and native consolidators. Daily consolidation follows exchange sessions, including
early closes. `Scan` flushes through the cutoff converted to exchange-local time.
Prefill emits no strategy decisions. The finite history span is a warmup attempt;
readiness still determines whether outputs are usable.

Unused consolidators/windows are removed. Adapter-owned manual subscriptions are
released through user-defined universe membership only when flat with no open
orders; the adapter does not call liquidation-oriented `RemoveSecurity` here.
Caller-supplied symbols retain caller ownership. Native universe membership and
holding/order retention remain LEAN's responsibility. Selected neutral membership
is distinct from retained resource ownership for residence, holdings or orders.

## Native indicators and late data

`IndicatorRequirement` declares a bars source with `lookback=1`, kind, immutable
parameter pairs, output names and output-window size. It grants no raw-bar access.

| Kind | Parameters | Outputs |
| --- | --- | --- |
| `sma` | Positive integer `period`; `field` defaults to close | `value` |
| `ema` | Positive integer `period`; optional `smoothing_factor`; `field` defaults to close | `value` |
| `rsi` | Positive integer `period`; native `moving_average_type` defaults to Wilder; `field` defaults to close | `value` |
| `donchian` | Positive integer `period`; complete bars | `value`, `upper`, `lower` |

Scalar fields are open/high/low/close/volume. Unknown kinds, parameters, fields or
outputs fail. Current Donchian bands include the current bar; a node needing the
preceding channel requests two points.

Kind, parameters and backing source identify a shared native indicator. Its
built-in windows use the largest owner request. One manual update path feeds it;
it is not also auto-registered. A temporary native consolidator warms the indicator
through initialization time. Warming raw windows does not feed it again. Enlarging
the output window rebuilds and warms the resource.

Readiness requires both native readiness and enough valid post-warmup outputs.
Snapshots contain chronological immutable points, readiness, native LEAN version,
settings, actual initialization start, last input end and admitted sequence.
Recursive indicator values can depend on the finite initialization range.
Sugarloaf supplies no formula fallback.

Raw windows retain the latest admitted revision per timestamp and preserve newer
requested observations when older data arrives. Exact duplicates do not change
values. Native indicators skip older samples with bounded
`NATIVE_INDICATOR_LATE_INPUT` diagnostics. Changed equal-time inputs follow native
equal-time update behavior. There is no late-data indicator replay ledger.

## Universes and lifecycle

Liquidity selection requires price, volume and dollar volume, filters thresholds,
then ranks descending dollar volume. ETF selection uses historical constituent
weights and declared missing-weight policy. Stable IDs break ties. Missing required
selection inputs leave membership unavailable. Unchanged and empty differ.

QC uses synchronous daily fundamental/ETF callbacks. Only default
`Trigger("universe")` refresh is supported; custom keyed, timed or interval refresh
fails explicitly. Duplicate native callbacks at one selection time return
`Universe.UNCHANGED`.

A changed definition under one neutral name creates a new native source.
Generation checks prevent obsolete callbacks publishing membership. Once the last
owner/reference disappears, the source is disposed. Its manager entry is collected
only after a later boundary and empty selected/member sets, preserving LEAN's
held/open-order retention.

A split rebuilds that instrument's data and indicator resources before reads. A
dividend rebuilds only those using `adjusted` or `total_return` prices; raw and
split-adjusted history is unaffected by a cash dividend.
Symbol changes preserve identity; delistings mark instruments untradable. Core
View invalidation and execution cancellation are specified separately.

## Account, costs and execution

The lazy QC account snapshot reads actual holdings, NAV, cash, positive prices
and open orders with ticket-filled quantities. Instrument lot size, multiplier,
currency and tradability come from the security. Valuation requires matching
instrument/account currencies; unavailable FX valuation fails explicitly.

Cost estimates equal absolute notional × configured proportional slippage plus
the native order-fee estimate converted through account cash-book FX. Zero
quantity costs zero. Missing/nonpositive fee FX conversion fails. Estimation does
not charge the account; LEAN applies actual costs during execution. Deployment
configures the same constant slippage and leverage through a security initializer.

Buying power uses LEAN's initial-margin model and available buying power. Borrow
follows LEAN's `IShortableProvider` contract: a number is the shortable quantity
and null means "shortable without restrictions". LEAN's default provider always
returns null, so a default backtest never runs out of borrow; configure a
shortable provider on the brokerage model to make borrow binding. The gateway supports regular-session day market orders,
tags orders with intent identity, and obtains feedback from platform tickets.

## QuantBook and memory providers

`QuantBookServices(qb, tickers=None, api=None)` supports explicit instruments, bars
and native indicators, with no account/execution services. Install requirements,
then call `prepare(start, end)` with aware times and a fresh provider. Preparation
silently warms through start and retrieves typed split/dividend history. Replay
admits neutral lifecycle facts at their effective boundaries; splits and dividends
reset affected data and native indicator resources before post-action observations
are admitted. Split warnings are not effective split facts. Future retrieved actions
are not exposed before their boundary. This local causal contract does not establish
native numerical parity or point-in-time adjustment-vintage correctness.

`history_batches(start, end)` lazily merges minute native feeds by end timestamp,
including calendar feeds. Daily-reading nodes receive deployment's intraday clock
cadence. One batch of native inputs is staged; runtime `apply` must precede the
next yield. Apply feeds and scans consolidators through that boundary, then admits
completed inputs. Resource updates rebuild future history iterators without
replaying admitted values. Retrieved future history is never visible to nodes.

Historical arrival equals observation end by assumption, not measured vendor
latency. `anchor_observation(instrument, at, convention, as_of)` separately queries
an exact minute close for outcomes, returns `None` when missing or later than
`as_of`, and leaves node inputs unchanged. Supported conventions are price
return excluding distributions and total return. The `price_return` convention
requests `split_adjusted` history so a mechanical split does not become a price
loss; `total_return` requests total-return history. Matching native adjustment
vintages across exact anchors and replay remains an acceptance check.

`sugarloaf.research.ResearchProvider` describes the structural interface accepted
by `run_node`: `resources`, installation, admission, Context creation, readiness,
schedule evaluation, preparation, and historical batches. It requires no provider
base class, QC objects, account service, or execution capability. Runtime installs
declarations before preparation and applies each batch before requesting another.
Provider metadata (`provenance`) is optional, and `apply` may return None when
the provider discovers no facts of its own. `MemoryServices` supplies fixture runtime services; a caller-provided
replay implementation adds preparation and historical-batch iteration. Outcome
resolution remains separate. See [the runner](../../sugarloaf/research/runner.py).

`MemoryServices` accepts explicit bars, quotes, selections, account
and session calendars. Writes invalidate Contexts; timestamp rules determine later
visibility. Native indicators are unsupported. Fixture buying power is a NAV bound
with reducing-order allowance; costs are proportional. `MemoryGateway` records
intents and cancellation requests; only explicit caller fills/cancel confirmations
change orders. It is not a market simulator.

## Failure semantics and limits

| Failure | Meaning |
| --- | --- |
| `ContractError` | Conflicting shared declarations or invalid replay lifecycle/staging |
| `CapabilityError` | Unsupported service/settings, undeclared read, incompatible subscription or unavailable conversion |
| `NotReady` | Incomplete/stale history, unavailable membership/anchor or unready indicator/window |
| `SnapshotError` | Context used after provider mutation or indicator containing unadmitted future points |

QuantBook rejects universe, quote
and portfolio declarations. Memory schedules need explicit calendar fixtures;
named timezone availability depends on local zone data, while UTC is built in.
QC uses native timezone and exchange databases. No timer creates synthetic market
observations to make schedules fire.

Injected-boundary tests establish integration/order behavior, not numerical parity
with LEAN or actual QuantBook kernel execution. Evidence and remaining checks are
in [Validation](../VALIDATION.md).


## Cloud tooling boundary

`sugarloaf.adapters.quantconnect.cloud` owns local packaging, project operations,
backtest requests and remote notebooks. These tools are separate from the adapter
inside LEAN. `sugarloaf.deployment` owns shared configuration, evidence and reporting.
The shared CLI selects `--adapter` and forwards remaining arguments unchanged;
QC tooling owns manifest parsing, options and the Python `run_backtest` interface.
The installed `sugarloaf run --adapter quantconnect --manifest PATH` command selects
that implementation. LEAN owns authentication, network
requests and retries. Local polling limits do not cancel jobs or time out requests.
[Deployment](DEPLOYMENT.md) documents factories, manifests, import rewriting,
source snapshots, historical lookup and log retrieval.
[External strategies](../EXTERNAL_STRATEGIES.md) explains optional package organization.

Adapter signatures are listed in the [API reference](../api/index.md).
