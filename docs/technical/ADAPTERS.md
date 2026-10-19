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
3. After a split, remove that instrument's raw-price consolidation/indicator
   state and return a `DataReset` naming the nodes that declared it (see
   [corporate actions](#corporate-actions-and-data-resets)).
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
non-equity/non-USA securities. Concurrent requests for one security must agree on
session, normalization and fill-forward semantics.

### Feed periods

Each security is subscribed at the coarsest native resolution that serves every
requirement on it, and no finer. LEAN delivers minute, hourly and daily trade
bars; `native_period(requirement)` is the largest of those that divides the
requested interval:

| Requirement | Native feed |
| --- | --- |
| Daily bars, regular session | Daily |
| 1-, 2-, 3-hour bars, regular session | Hourly |
| 5-minute, 90-minute bars | Minute |
| Any quotes; any extended-session request | Minute |

- **The finest reader decides.** A security read daily by one node and at five
  minutes by another has one minute subscription; the daily window is then built
  from minutes by LEAN's session-calendar consolidator.
- **LEAN does the consolidation.** Every window is fed by a native consolidator
  registered on the security, which LEAN updates. When the feed already has the
  requested interval that consolidator is `IdentityDataConsolidator`, which
  passes each bar through as it arrives; otherwise it is `TradeBarConsolidator`
  or `QuoteBarConsolidator`. The adapter updates a consolidator by hand only to
  warm a new window from history, and in QuantBook replay.
- **A universe adds its members with its readers' settings.** The native universe
  source is created with the feed period, session, normalization and fill-forward
  of the requirements that select it, so a member carries one subscription. A
  universe whose members nobody reads prices from adds them daily. When a reader
  of a finer interval is installed the source is recreated.
- **A schedule's calendar security has a minute feed.** A schedule is checked when
  data arrives, so the security whose sessions it follows must tick each minute.
  It owns that feed without granting nodes price-read permission.
- **A security named by a node starts at the finest universe period.** LEAN feeds
  a consolidator from the first subscription a security was given and ignores
  resolution (observed natively, see [validation](../VALIDATION.md)). A named
  security is subscribed before any universe can select it, so it takes the finer
  of its own period and the finest universe period at once.
- **A feed is not made finer in place.** If a security already carries a coarser
  subscription than a new requirement needs, the adapter raises `CapabilityError`
  instead of serving bars of the wrong interval. Declare the finer interval from
  the start. A feed is not made coarser while anything still reads the security.

The graph clock follows the feeds. A graph that reads only daily bars receives one
data boundary per session, at the close; nothing in it can act at 10:30. A node
that must act inside the session declares the data it acts on (minute bars for
"at 15:50") or a session-calendar schedule. Daily bars end at the session close
(`daily_precise_end_time`, set explicitly by the launcher).

Prefill uses typed history ending at an explicit cutoff. Regular daily requests
always use daily history, whatever the live feed; other requests use history at
the feed's period and native consolidators. Daily consolidation from an intraday
feed follows exchange sessions, including
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

A `FundamentalUniverse` uses the same daily fundamental callback as liquidity
selection; its rule receives one record per security from the callback's objects
(CIK from the company reference, market cap, price, dollar volume). Observed in a
backtest (March 2024): the callback runs at 00:00 each day, Saturdays included, with
records of the previous close; a security first selected at 00:00 has its
fundamentals and daily history at the first bar after the open, while its own
`price` stays 0 until its first bar arrives. QuantConnect documents live
fundamental selection at 7–8 AM Eastern.

Fundamental reads come from each subscribed security's `fundamentals` record, which
LEAN keeps point-in-time; a security must be subscribed (a universe member or a bar
reader) to have one. Borrow terms come from the security's shortable provider: `shortable_quantity`
and `fee_rate` (Interactive Brokers data, for example). LEAN's default provider
reports a zero fee and unlimited quantity for everything; the adapter treats that
as no data and returns no record. The brokerage model does not attach Interactive
Brokers' provider even under its own name: set `Environment(borrow_data=
"interactive_brokers")` and the security initializer gives every security,
including later universe members, that provider. The same setting gives each security
LEAN's `ShortMarginInterestRateModel`, which charges the provider's fee rate on short
holdings and credits its rebate; LEAN's default model charges nothing. Records are stamped with the time LEAN read them,
so a read after the Context's cutoff is refused; their `max_age` therefore bounds
read lag, not the vendor's own update date. A rule that raises `NotReady` leaves
the universe unready and its members subscribed.

Symbol changes preserve identity; delistings mark instruments untradable.

## Corporate actions and data resets

Whether a corporate action makes retained prices unusable depends on the vendor,
so each adapter owns that rule. An adapter that discards and rebuilds data returns
`Event("data_reset", payload=DataReset(instrument, nodes, effective_at))` from
`apply`; `ProviderServices._data_reset` builds it from the rebuilt resource keys.
The [runtime](RUNTIME.md#viewstore-and-lifecycle) withdraws and re-evaluates the
named nodes. An adapter that rebuilds nothing returns nothing.

The QuantConnect rule, for backtests and QuantBook research:

| Fact | `adjusted`, `split_adjusted`, `total_return` | `raw` bars, quotes |
| --- | --- | --- |
| Dividend | Nothing rebuilt | Nothing rebuilt |
| Split | Nothing rebuilt | Windows and indicators rebuilt; `DataReset` reported |

LEAN adjusts those series with the whole factor file from the first bar, so they
are continuous through an ex-date or a split and do not change on it. The level of
an adjusted price is therefore not the price traded on its date: in a March 2024
backtest, NVDA's adjusted close was about 85 against about 850 raw, because of the
10-for-1 split of June 2024. Returns from adjusted bars are right; a price level
(a price filter, a market value) needs raw bars or a fundamental record's price. A raw price
that opens lower by a dividend is a real price move. Only a split puts raw prices
on a different per-share scale. The rebuilt raw history is still as traded: a
window spanning the split mixes both scales, so the rebuild restarts state but
does not rescale old prices. A strategy that needs continuous prices should
request an adjusted convention.

Independently of any reset, the runtime withdraws Views stated in units of a
split instrument (a quantity position, a price-level claim), under every
convention. Weight positions, rankings and return claims stay unless their node's
data was reset. LEAN's own holdings adjustment remains native.
This rule does not describe live trading, which is unsupported.

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
The initializer also gives a new security its last known price. Without it a
security has no price until its first bar, which on a daily feed is the whole day
it joins a universe, and LEAN refuses orders for it.

Buying power uses LEAN's initial-margin model and available buying power. Borrow
follows LEAN's `IShortableProvider` contract: a number is the shortable quantity
and null means "shortable without restrictions". LEAN's default provider always
returns null, so a default backtest never runs out of borrow and pays no borrow
fee; `Environment(borrow_data="interactive_brokers")` makes borrow binding and charged.

The gateway supports regular-session `day` or `gtc` orders of three types
(`market`, `market_on_open`, `market_on_close`), tags orders with intent
identity, and obtains feedback from platform tickets. The host hands each order
event to the engine as it arrives and runs any further reconciliation after the
current one ends. It chooses the native call and nothing
else: when an order may be sent and how it fills are LEAN's rules, set by the
backtest's brokerage and fill models
([execution](EXECUTION.md#order-types-and-timing)). LEAN refuses some orders
before creating them, returning an invalid ticket with no order and no order
event; the gateway reads that from the ticket and raises `OrderRefused`.

## QuantBook and memory providers

`QuantBookServices(qb, tickers=None, api=None)` supports explicit instruments, bars
and native indicators, with no account/execution services. Install requirements,
then call `prepare(start, end)` with aware times and a fresh provider. Preparation
silently warms through start and retrieves typed split/dividend history. Replay
admits neutral lifecycle facts at their effective boundaries; a split resets
raw-price data and native indicator resources before post-action observations
are admitted, as [above](#corporate-actions-and-data-resets). Split warnings are
not effective split facts. Future retrieved actions
are not exposed before their boundary. This local causal contract does not establish
native numerical parity or point-in-time adjustment-vintage correctness.

`history_batches(start, end)` lazily merges native feeds by end timestamp,
including calendar feeds. Each security is replayed at the
[feed period](#feed-periods) a deployment would subscribe it at, so the replay
ticks when the deployment does: a node that reads only daily bars is evaluated at
each close. One batch of native inputs is staged; runtime `apply` must precede the
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
