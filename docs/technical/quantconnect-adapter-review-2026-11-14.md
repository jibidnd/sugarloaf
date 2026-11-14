# QuantConnect adapter review, 14 November 2026

A review of `sugarloaf/adapters/quantconnect/` against LEAN's own idioms, with a redesign
proposal. It covers the adapter at commit `bb6aaa1` (after the history-prefill fix of
`2a9f6ad`), LEAN master `82d2604` of 13 November 2026, QuantConnect's documentation as
served on 14 November 2026, and the forum threads cited inline. The measurements are
QuantConnect backtests run for this review in project 39378262 on a B2-8 node, with their
scripts and result files under [tools/qc_adapter_review/](../../tools/qc_adapter_review/).

Each statement below is marked **verified** (observed in a run, or read in the cited
source) or **inferred** (reasoned from code without a run). The 10-minute figure that
started this review is a LEAN rule, not a platform mystery: one time loop may take ten
minutes, scheduled and training events can extend it, and the adapter gave a node no way
to use either.

## 1. Summary

The adapter rests on LEAN's own mechanisms more than the history incident suggested:
subscriptions, universes, consolidators, indicators, corporate-action events, fundamentals,
orders and tickets are all LEAN's calls, used as the documentation describes. What the
adapter re-implements sits in one layer: everything LEAN hands it is converted, one object
at a time in Python, into neutral values and kept in Python collections, and every read
filters those collections again. That layer is where the cost is, and it grows with the
size of the panel and the lookback, which is why a 214 × 4,900 daily panel exposed it.

Measured on the panel the move-size node declares (section 5 has the runs):

| Operation | LEAN's own call | Through the adapter | Verified in |
| --- | --- | --- | --- |
| 214 securities × 4,900 daily bars, one history call | 5.4 s to read and enumerate 756,082 bars; 5.4 s reading every field in Python; 8.0 s with the adapter's two time conversions per bar | about 8.7 s to prefill the members' windows in the first data callback, plus 1.8 s for the 15 named securities at construction (unprofiled); 25.9 s and 4.6 s profiled, three quarters of it building neutral `Bar` values | D1, D3 |
| The same bars, one call per security | 3.9 s for 214 calls | the adapter's path before `2a9f6ad` | D1 |
| The same bars as a DataFrame | 5.8 s (plus 0.2 s to unstack closes) | not used | D1 |
| EODHD earnings calendar, 6,800 days, whole market | 374 s as the DataFrames the adapter reads (1.9 million rows), of which 4.3 s is the adapter's pandas reduction; 0.55 s per year in the typed list shape | 142 s on the first evaluation unprofiled, 439 s profiled; the Lab saw 326 s; all of it LEAN's DataFrame assembly | D1, D2, D3 |
| One session's reads of the loaded panel | 0.5 s to warm 214 SMA(252) indicators with `warm_up_indicator`; 0.1 s to read 200 fundamental records | 0.6–1.4 s per session unprofiled: two 0.21 s passes over 760,000 retained bars, and 0.05–0.93 s extending the earnings calendar by a day through the DataFrame read | D3 |
| LEAN warm-up of 4,900 daily bars for 215 securities | 10.4 s for 754,902 bars | not used by the adapter | D2 |
| The earnings calendar, one year, typed `history[EODHDUpcomingEarnings]([symbol], start, end, DAILY)` | 0.55 s for 164,641 rows | the adapter reads the DataFrame shape: 57.9 s for the same year | D2 |
| The earnings calendar, all 6,800 days, typed, filtered to the panel in Python | 9.2 s for 1,850,087 rows (80,396 of the panel) | 142–374 s through the DataFrame shape | D2b |
| LEAN warm-up of the same bars with the earnings calendar streamed too | 16.4 s; 1,899,912 listings delivered | not used by the adapter | D2b |

The proposal (section 4) reads the earnings calendar in LEAN's typed shape (nine seconds
for what the DataFrame shape takes minutes over), removes the per-bar Python conversion
and the per-read re-filtering, moves long work under LEAN's `train` budget, and uses
`indicator_history` and `SCALED_RAW` where the adapter re-implements LEAN's warm-up. It keeps the neutral contracts, the
cutoff and receipt discipline, per-requirement readiness, the feed planning, the
corporate-action rule and the execution gateway. Nodes see no change in the Context API;
two additions are proposed (a columnar bar read and a declared long-running step).
Section 6 lists the decisions this asks of Bobby.

## 2. Responsibilities: current mechanism, LEAN's mechanism, gap

References into the adapter are to commit `bb6aaa1`; into LEAN, to master `82d2604`
(paths under `https://github.com/QuantConnect/Lean/blob/82d26047e95a89b53c9cbe02b67efaa3c4455ed9/`).

### 2.1 Universe and security addition

**Current.** A `UniverseRequirement` becomes one native fundamental universe
([lean.py:312](../../sugarloaf/adapters/quantconnect/lean.py#L312) `universe`), created with a
copy of `universe_settings` whose resolution, fill-forward, normalization and extended-hours
come from the requirements that read the members, and `minimum_time_in_universe` from the
definition. Selection runs synchronously. The selector
([services.py:261](../../sugarloaf/adapters/quantconnect/services.py#L261)) returns
`Universe.UNCHANGED` for a repeated call at one time and inside an already selected cadence
period; otherwise it converts every row to a neutral `SelectionRow` or `FundamentalRecord`,
binding each symbol by its security identifier, ranks with the shared `select`, publishes
membership and queues a `universe` event for the next data boundary. Named securities go
through `add_security` with the resolution the finest reader needs
([lean.py:95](../../sugarloaf/adapters/quantconnect/lean.py#L95)); a security initializer
sets slippage, leverage, borrow data and seeds the last known price
([lean.py:62](../../sugarloaf/adapters/quantconnect/lean.py#L62)). A universe whose readers
change feed settings is recreated under a new generation and the old one disposed and
collected later ([services.py:200](../../sugarloaf/adapters/quantconnect/services.py#L200)).
Manual subscriptions are released through the user-defined universe rather than
`remove_security`, which would liquidate
([lean.py:359](../../sugarloaf/adapters/quantconnect/lean.py#L359)). Only LEAN's own daily
refresh is supported; cadence is enforced inside the selector.

**LEAN.** `add_universe(filter)` with `universe_settings` for resolution, fill-forward,
normalization, extended hours, leverage, `minimum_time_in_universe` (default one day),
`asynchronous` and `schedule.on(date_rule)`
([Universes › Settings](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/settings)).
`Universe.UNCHANGED` keeps the members; `universe.selected` versus `universe.members`
distinguishes selection from retention of held, ordered or recently added securities
([Universes › Key Concepts](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/key-concepts);
`Engine/DataFeeds/UniverseSelection.cs` L142–251, `Common/Data/UniverseSelection/Universe.cs`
L163–190 for `CanRemoveMember`). Members are created with the universe's settings
(`UniverseSelection.cs` L293–296). `settings.seed_initial_prices = True` makes the default
security initializer seed every new security
([Initialization](https://www.quantconnect.com/docs/v2/writing-algorithms/initialization)).
Fundamental selection runs at midnight in backtests and 7–8 AM Eastern live, with about
5 MB of RAM per selected asset
([Fundamental Universes](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity/fundamental-universes)).

**Gap.** *Cost:* selection work happens only on cadence boundaries; on those days the
selector touches about 8,000 rows in Python (3.9 s for 8,923 rows in D1, verified). On every
other day LEAN still loads the day's fundamental file and calls the selector, which
returns `UNCHANGED`; `universe_settings.schedule.on(...)` would let LEAN skip those days
entirely (inferred; not measured). *Correctness:* membership periods follow the UTC date
of the selection, documented in [MARKET.md](MARKET.md); no look-ahead found. The security
initializer re-implements `seed_initial_prices` with a `get_last_known_prices` loop; LEAN
offers the setting (verified in the docs; both do one history read per new security).
*Maintenance:* the generation, retirement and collection machinery (~120 lines) exists
because requirements can change a universe's feed settings after creation, which LEAN does
not support in place. It is Sugarloaf's declaration model, not a re-implementation.

### 2.2 History and warm-up

**Current.** Resources are warmed by explicit history requests at every resource change
([services.py:424](../../sugarloaf/adapters/quantconnect/services.py#L424) `_sync_resources`),
never by LEAN's warm-up. Regular daily windows are read in bulk, one typed history call per
(normalization, fill-forward) group over `count × 1.45 + 10` calendar days ending at the
cutoff ([lean.py:207](../../sugarloaf/adapters/quantconnect/lean.py#L207) `daily_histories`),
then converted bar by bar into neutral `Bar` values with two exchange-to-UTC conversions
and six field reads each ([services.py:660](../../sugarloaf/adapters/quantconnect/services.py#L660)
`_fill_window`). Every other window is read per security over `count × 3` days (or the
minute equivalent) and pushed through a native consolidator one bar at a time
([lean.py:174](../../sugarloaf/adapters/quantconnect/lean.py#L174),
[services.py:586](../../sugarloaf/adapters/quantconnect/services.py#L586)). Indicators are
warmed separately with their own history per instrument (section 2.4). Bars ending after
the cutoff are dropped, so no retrieval can leak the future; the first evaluation happens
at the first completed bar after the start, never on history
([VALIDATION.md](../VALIDATION.md), parity section).

**LEAN.** Direct history in four shapes, with the typed shapes documented as cheaper than
DataFrames ("Otherwise, LEAN consumes unnecessary computational resources populating the
DataFrame", [History Responses](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-responses)).
A bar-count request derives the span from each security's trading calendar
([History Requests](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-requests)).
One multi-symbol request is served by one reader per symbol merged in C# by end time
(`Engine/HistoricalData/SubscriptionDataReaderHistoryProvider.cs` L88–190,
`SynchronizingHistoryProvider.cs` L56–112), so one call for 214 symbols costs one merge
rather than 214 round trips, the point staff made in forum threads
[4735](https://www.quantconnect.com/forum/discussion/4735/runtime-error-algorithm-took-longer-than-10-minutes-on-a-single-time-loop/)
and [813](https://www.quantconnect.com/forum/discussion/813/feature-history-and-warmup/p3).
Indirect history is `set_warm_up(bars, resolution)`, which replays data through `on_data`,
consolidators, indicators and universe selection before the start date
([Warm Up Periods](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)),
and the docs' own trade-off: one line of code, but all subscriptions are replayed.

**Gap.** *Cost:* LEAN's read of the panel is about 5 s in any shape (D1) and its own warm-up of the same bars 10 s (D2); through the adapter the members' prefill is about 9 s unprofiled and 26 s profiled, three quarters of it building neutral `Bar` values one by one (D3), so the adapter's conversion layer costs about as much again as LEAN's read. Locally, one `Bar` with its two time conversions and validity checks costs 3.6 µs on CPython 3.13, 2.7 s per 760,000 bars before Python.NET; on the node with real conversions it is roughly twice that. *Correctness:* the calendar-day span guess
(`1.45 × count + 10`) can fall short of `count` sessions after long closures, where a
bar-count request cannot; `min_lookback` masks the shortfall for recent listings only.
End-time handling is right: history endpoints are the cutoff converted to the algorithm
zone, bar times are converted from the exchange zone (both verified against
[Time Zones](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/time-zones)).
*Maintenance:* about 250 lines of prefill and admission logic stand in for `set_warm_up`
and `indicator_history`; the structural cost is the per-bar Python object model, not any
single loop.

### 2.3 Consolidation and bar admission

**Current.** Native consolidators, registered on the security with
`subscription_manager.add_consolidator`: LEAN's identity consolidator when the feed has the
requested interval, otherwise `TradeBarConsolidator`/`QuoteBarConsolidator`, with a
session-calendar function for daily bars built from intraday feeds
([lean.py:143](../../sugarloaf/adapters/quantconnect/lean.py#L143)). Each completed bar
runs a Python handler that converts its end to UTC through Python.NET, queues the bar and a
`bar_close` event ([services.py:561](../../sugarloaf/adapters/quantconnect/services.py#L561)).
At `apply`, each queued bar is converted to a neutral value (two more conversions, six
fields) and admitted to a `deque` after a linear search of the whole window for an equal
timestamp ([services.py:928](../../sugarloaf/adapters/quantconnect/services.py#L928)
`_admit_bar`). The host decides the cutoff from the slice's bar ends, holds
callback-queued facts until a slice carries data, and admits during LEAN's warm-up without
decisions ([host.py:206](../../sugarloaf/adapters/quantconnect/host.py#L206)).

**LEAN.** Consolidators plus the `consolidate` helper; each consolidator keeps a rolling
window of its output; scheduled events run before consolidation handlers, which run before
`on_data` ([Consolidating Data](https://www.quantconnect.com/docs/v2/writing-algorithms/consolidating-data/getting-started),
`Common/Data/Consolidators/PeriodCountConsolidatorBase.cs` L262 for `Scan`). Retained
history is a `RollingWindow` (O(1) add) or an indicator window, not a searched list.

**Gap.** *Cost:* the dedupe search is O(lookback) per admitted bar, the same shape the
prefill had before `2a9f6ad`; with 214 instruments and a 4,900 window it is about a million
comparisons per session: 0.57 ms per bar and 0.12 s per session measured locally on the
adapter's code, 4,428 comparisons per bar in the D3 profile (verified; a small cost today
that grows with lookback × instruments × bars a day). Per-bar Python.NET conversions are
three interop calls per bar, negligible for daily feeds and about 250,000 calls a day for
214 minute feeds (inferred from the call count; the minute case was not run here).
*Correctness:* the consolidator choice, the cutoff from bar ends and the held facts on
data-less slices are right and were verified natively on 9–12 October
([VALIDATION.md](../VALIDATION.md)). *Maintenance:* the window and dedupe code is
Sugarloaf's late-data contract and should stay; only its data structure is wrong.

### 2.4 Indicators

**Current.** SMA, EMA, RSI and Donchian are created manually
([lean.py:461](../../sugarloaf/adapters/quantconnect/lean.py#L461)), warmed by a temporary
consolidator fed from a per-instrument history request
([services.py:802](../../sugarloaf/adapters/quantconnect/services.py#L802) `_sync_indicators`),
updated only by the adapter's admission path with late-input diagnostics and identical-input
suppression ([services.py:870](../../sugarloaf/adapters/quantconnect/services.py#L870)),
and read from the native output windows with readiness requiring both `is_ready` and enough
points after the first ready sample
([services.py:965](../../sugarloaf/adapters/quantconnect/services.py#L965)).

**LEAN.** Automatic indicators (`self.sma(symbol, 20, Resolution.DAILY)`), manual ones
registered with a consolidator and a field selector, `warm_up_indicator`,
`indicator_history(indicator, symbol, start, end, resolution, selector)` (which resets and
warms the indicator from history in C#; `Algorithm/QCAlgorithm.Indicators.cs` L3434–3480
and L4112–4190), `settings.automatic_indicator_warm_up`, `window.size`, `deregister_indicator`
([Indicators › Key Concepts](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/key-concepts),
[Manual Indicators](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/manual-indicators),
[Automatic Indicators](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/automatic-indicators)).

**Gap.** *Cost:* small; indicators are few and warmed once (D1 measured LEAN's helper at
0.46 s for 214 SMA(252) indicators, and `indicator_history` at 0.03 s for 4,900 points, verified). *Correctness:* equivalent; the
adapter's explicit end is what `indicator_history(…, start, end)` gives. *Maintenance:* the
warm loop with a temporary consolidator re-implements `WarmUpIndicatorImpl`; the manual
update path is Sugarloaf's admission discipline and should stay.

### 2.5 Scheduled work and long computations

**Current.** Everything a graph does runs inside `on_data`: resource prefill at a boundary,
node evaluation, order reconciliation. Sugarloaf schedules are checked against exchange
hours when data arrives ([lean.py:537](../../sugarloaf/adapters/quantconnect/lean.py#L537)),
with the calendar security given a minute feed so a time inside the session can tick. There
is no use of `train`, of `schedule.on` or of the training budget. The move-size node's
first evaluation (history load plus earnings calendar) took 328 s and a yearly refit 248 s
inside one `on_data` (Lab `backtest_check_results.json`, runs `477134e1…` and `68f9ccf3…`,
verified); before `2a9f6ad` the same first evaluation exceeded ten minutes four times.

**LEAN.** One time loop may take ten minutes
([Training Models](https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/training-models);
`Engine/AlgorithmTimeLimitManager.cs` L214 for the message, `Engine/AlgorithmManager.cs`
L194 for the reset at each time slice). `train(fn)` or `train(date_rule, time_rule, fn)`
runs `fn` as a scheduled event; while it runs, a `TimeMonitor` asks the limit manager for
one more minute each minute from a leaky bucket (`Common/Scheduling/TimeMonitor.cs` L96–115,
`Common/IsolatorLimitResultProvider.cs`), and ordinary scheduled events draw from the same
bucket ([Resources › Training Quotas](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources#08-Training-Quotas):
B-MICRO 20 min capacity and 1 min/day refill, B2-8 30 and 5, B4-12 60 and 10, B8-16 90
and 15, refilled in real time). `train(fn)` called now schedules the event for
`DateRules.Today, TimeRules.Now` (`Common/Scheduling/ScheduleManager.cs` L228); in a
backtest the realtime handler fires due events at the start of the next time step, before
`on_data` (`Engine/RealTime/BacktestingRealTimeHandler.cs` L100–149;
[Scheduled Events](https://www.quantconnect.com/docs/v2/writing-algorithms/scheduled-events)
documents the order and recommends a training session for events over ten minutes). The
forum's practitioner pattern for heavy daily work is to split it across scheduled events,
each with its own window ([19378](https://www.quantconnect.com/forum/discussion/19378/algorithm-took-longer-than-10-minutes/),
2025), and to do large history loads outside the data loop
([11226](https://www.quantconnect.com/forum/discussion/11226/loops-longer-than-10-minutes-on-live/)).

**Gap.** *Correctness of the premise:* the ten minutes are a budget per time loop with
documented extensions; treating it as a fixed platform constraint on a node's step was the
adapter's omission, not LEAN's rule. D2 verified the mechanism in a daily backtest: a `train` callback requested inside the 16:00 `on_data` ran at the start of the next time step with the algorithm clock set back to 16:00, LEAN warned after one minute that the event "has been executing for over 1 minute(s)" and would be stopped once the loop limit was exhausted, and the 75-second callback completed. *Cost:* none in the adapter; the cost is
the node's. *Maintenance:* adding a declared long-running step is a host change (section 4).

### 2.6 Fundamentals and alternative data

**Current.** Fundamentals are read from `security.fundamentals`, LEAN's point-in-time
record, stamped at read time ([services.py:1100](../../sugarloaf/adapters/quantconnect/services.py#L1100)).
The earnings calendar is QuantConnect's EODHD Upcoming Earnings dataset, added once as a
custom symbol and read through `history` as a DataFrame a calendar year at a time over the
longest lookback any node asked for, reduced in pandas to one listing per security and day
and then to changes of listing, and kept per security identifier
([lean.py:625](../../sugarloaf/adapters/quantconnect/lean.py#L625),
[services.py:1109](../../sugarloaf/adapters/quantconnect/services.py#L1109)). The load is
lazy: it happens inside the first evaluation that reads earnings, and later reads extend it
by the days since. Borrow terms come from the security's shortable provider.

**LEAN.** The dataset is "a daily universe of US Equities with an earnings report
publication in the upcoming 7 days", "designed for universe selection", added with
`add_universe(EODHDUpcomingEarnings, filter)` or `add_data`, with history by span
([Upcoming Earnings](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/eod-historical-data/upcoming-earnings)).
Its source is one whole-market file per day
(`Lean.DataSource.EODHD/EODHDUpcomingEarnings.cs`, `GetSource`), so there is no per-security
history: any read of N years parses N × 252 market-wide files, whatever the symbol list.
Fundamentals: `security.fundamentals`, `self.fundamentals(symbols)`, `history(Fundamental, …)`
([US Fundamental Data](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/morningstar/us-fundamental-data)).

**Gap.** *Cost:* the whole first-session cost is the read shape. The DataFrame shape the adapter uses costs 57.9 s for the year 2020 and 374 s for the 19-year span in plain LEAN (D1, D2), because LEAN assembles one frame per reporting company and joins them in pandas (section 5.2); the adapter's own reduction is 4.3 s. The typed shape `history[EODHDUpcomingEarnings]([symbol], start, end, Resolution.DAILY)` returns the same year in 0.55 s and the whole 19-year span in 9.2 s (D2, D2b), and LEAN's own warm-up streams the whole span in about 6 s (D2b). Through the adapter the first evaluation spent 439 s here (D3, profiled). *Correctness:* the adapter stamps a listing at the
start of the file's day in New York, which is when LEAN would emit it (`EndTime` is the file
date, verified in the data source); a moved date appears as a new record; no look-ahead
found. *Maintenance:* the year loop, the reduction and the span cache are ~90 lines that exist
because the dataset has no per-security read; they stay, and the typed read replaces only
the DataFrame call and the pandas steps that served it.

### 2.7 Corporate actions

**Current.** `slice.splits`, `dividends`, `symbol_changed_events` and `delistings` become
`LifecycleEvent`s ([host.py:171](../../sugarloaf/adapters/quantconnect/host.py#L171)); split
warnings are ignored. A split on raw prices or quotes rebuilds that instrument's windows
and indicators from raw history and reports a `DataReset`; adjusted conventions and
dividends rebuild nothing, because LEAN applies the whole factor file from the first bar
([services.py:391](../../sugarloaf/adapters/quantconnect/services.py#L391), verified
natively in October). Holdings adjustments stay LEAN's. Research reads `history[Split]` and
`history[Dividend]` ([lean.py:687](../../sugarloaf/adapters/quantconnect/lean.py#L687)).

**LEAN.** The same objects, at midnight Eastern in backtests. With `RAW` data the
documentation says to reset indicators and consolidators on a split or dividend and warm
them with `SCALED_RAW` history, the normalization that scales past prices to the current
share basis and exists only for history requests
([Corporate Actions](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions),
[US Equity › Requesting Data](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data)).

**Gap.** *Correctness:* the adapter rebuilds a raw window with raw history, so a window that
spans the split holds two per-share scales (documented in
[ADAPTERS.md](ADAPTERS.md#corporate-actions-and-data-resets)); LEAN's idiom gives a
continuous series. *Cost:* negligible. *Maintenance:* the rule itself is right where it is.

### 2.8 Orders and fills

**Current.** Market, market-on-open and market-on-close orders submitted asynchronously
with the intent identity as tag and a time in force
([lean.py:406](../../sugarloaf/adapters/quantconnect/lean.py#L406)); refusals read from the
returned ticket; cancellation through `transactions.cancel_order` with the response checked;
fills read as the ticket's cumulative filled quantity on every order event, queued while a
decision runs and drained before reconciliation
([host.py:285](../../sugarloaf/adapters/quantconnect/host.py#L285)); buying power from the
security's buying-power model and `portfolio.get_buying_power`; fees from the fee model
([lean.py:377](../../sugarloaf/adapters/quantconnect/lean.py#L377)). The account snapshot
iterates every security at each read.

**LEAN.** The same calls; `set_holdings`/`PortfolioTarget` and the framework's execution
models are the higher-level idiom
([Order Events](https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/order-events)).

**Gap.** None material. Partial fills, reservations and cancel semantics are Sugarloaf's
execution contract and justify the gateway. The whole-run overhead of the adapter on a
daily cross-sectional strategy is on record: 48 minutes against 33 for a plain algorithm
over thirteen years ([VALIDATION.md](../VALIDATION.md), verified), with 35.8 million data
points, and the ordering of fills and reconciliation was verified in October.

### 2.9 Research-notebook access

**Current.** `QuantBookServices` replays history causally: per-security typed history
iterators merged in a heap by end time, one batch of simultaneous bars staged at a time,
pushed through the native consolidators and admitted at `apply`, with split and dividend
facts at their effective boundaries and outcome anchors read from minute history
([research.py](../../sugarloaf/adapters/quantconnect/research.py)).

**LEAN.** `qb.history(symbols, start, end, resolution)` DataFrames, `qb.universe_history`,
`qb.indicator_history`; look-ahead is the researcher's responsibility
([Research › Datasets](https://www.quantconnect.com/docs/v2/research-environment/datasets/key-concepts)).

**Gap.** *Cost (inferred, not measured here):* every bar costs a heap operation, a
consolidator update through Python.NET and an admission; for the 214 × 4,900 panel that
is about a million bars, minutes in a kernel. The acceptance notebook replays hundreds.
*Correctness:* the point of the design, running the same node code with no look-ahead, is
Sugarloaf's and worth keeping. *Maintenance:* 350 lines; batching admission by day would
keep the contract.

### 2.10 Logging and Object Store

**Current.** `SUGARLOAF_RUN` and `SUGARLOAF_RESULT` go out as JSON through `debug`; study
records through `log`, under 8,000 characters each, universe members a hundred per record
([launcher.py:15](../../sugarloaf/adapters/quantconnect/launcher.py#L15),
[launcher.py:135](../../sugarloaf/adapters/quantconnect/launcher.py#L135)). Factories can
read the Object Store (`stored`) and fetch URLs; nothing writes to the store.

**LEAN.** Debug messages are limited to 200 characters and rate limited; logs are capped
per backtest and per day by tier (100 KB and 3 MB on Quant Researcher, 1 MB and 10 MB on
Team), 8,192 characters a line, and reading logs counts against the daily quota
([Logging](https://www.quantconnect.com/docs/v2/writing-algorithms/logging),
[Resources › Log Quotas](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources#09-Log-Quotas)).
The Object Store is the documented place for results, caches of expensive history and
trained models, written once in `on_end_of_algorithm` and read back in research
([Object Store](https://www.quantconnect.com/docs/v2/writing-algorithms/object-store),
`Algorithm.Python/ObjectStoreExampleAlgorithm.py`).

**Gap.** *Correctness:* the result record through `debug` exceeds the documented 200
characters; it has arrived intact in every retained log, so the limit is the terminal's,
but it is undocumented behaviour to lean on (inferred). *Cost:* a study's records compete
with the log quota; the Lab's checks already split them to fit. *Maintenance:* a
store-backed result file and a `stored` writer are small additions.

## 3. The LEAN idioms the adapter should rest on

1. **One history call per group of symbols, in a typed shape.** `history[TradeBar](symbols,
   start, end, Resolution.DAILY)`; LEAN merges one reader per symbol in C#
   (`SubscriptionDataReaderHistoryProvider.cs` L88–190; [History Responses](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-responses)).
   The adapter does this for daily windows since `2a9f6ad`.
2. **Bar-count history for warm-up lengths.** `history[TradeBar](symbols, 4900,
   Resolution.DAILY)` lets LEAN count sessions on each security's calendar
   ([History Requests](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-requests)).
3. **`indicator_history` and `warm_up_indicator` for indicators.**
   `indicator_history(indicator, symbol, start, end, resolution, selector)` resets and
   warms in C# with an explicit end (`QCAlgorithm.Indicators.cs` L4112–4190;
   [Indicators › Key Concepts](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/key-concepts)).
4. **`SCALED_RAW` to rebuild after a split on raw data**
   ([Corporate Actions](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions)).
5. **`train` for any step that may exceed a few minutes**, with the node class's bucket in
   mind; scheduled events for work that can be split
   ([Training Models](https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/training-models),
   [Training Quotas](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources#08-Training-Quotas),
   `Algorithm.Python/TrainingExampleAlgorithm.py`).
6. **`universe_settings.schedule` for cadenced universes** and `Universe.UNCHANGED`
   otherwise ([Universes › Settings](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/settings)).
7. **`settings.seed_initial_prices`** instead of a seeding loop
   ([Initialization](https://www.quantconnect.com/docs/v2/writing-algorithms/initialization)).
8. **Rolling windows, not searched lists, for retained bars**; consolidators keep their own
   ([Consolidating Data](https://www.quantconnect.com/docs/v2/writing-algorithms/consolidating-data/getting-started)).
9. **Stream alternative data, or read it once and cache it.** The earnings dataset is a
   universe-style daily feed ([Upcoming Earnings](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/eod-historical-data/upcoming-earnings));
   expensive derived tables belong in the Object Store
   (`ObjectStoreExampleAlgorithm.py`; [Object Store](https://www.quantconnect.com/docs/v2/writing-algorithms/object-store)).
10. **Results to the Object Store, logs for summaries**, within the log quotas
    ([Logging](https://www.quantconnect.com/docs/v2/writing-algorithms/logging)).
11. **Market data through subscriptions and consolidators, decisions on bar ends**
    (`daily_precise_end_time`, [Periods](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/periods),
    [Timeslices](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices)),
    which the adapter already follows.

## 4. Redesign proposal

### 4.1 Remove, because LEAN provides the operation

| Wrapper code | Replaced by | Nodes see |
| --- | --- | --- |
| Per-bar neutral conversion at prefill ([services.py:660](../../sugarloaf/adapters/quantconnect/services.py#L660)) and per-bar time conversion through Python.NET | Bulk conversion of a typed history call into column arrays (time, open, high, low, close, volume, fill-forward flag) per instrument, with exchange-to-UTC offsets computed once per session day; neutral `Bar` objects built on read, lazily | Nothing; `ctx.market.bars` returns the same `Bar` values |
| Linear dedupe search in `_admit_bar` | Compare with the newest retained stamp first; search only for an older arrival | Nothing |
| The indicator warm loop with a temporary consolidator ([services.py:852](../../sugarloaf/adapters/quantconnect/services.py#L852)) | `indicator_history(indicator, symbol, start, cutoff, resolution, selector)` through `LeanAPI` | Nothing |
| Raw history for split rebuilds | `SCALED_RAW` history for the rebuilt windows and indicators | A raw reader's window is continuous across the split, on the current share basis |
| The seeding loop in the security initializer | `settings.seed_initial_prices = True` | Nothing |
| The calendar-day span guess for daily prefill | A bar-count request where the feed is daily | Nothing; fewer short windows after long closures |
| The yearly DataFrame read of the earnings calendar and its pandas reduction ([lean.py:625](../../sugarloaf/adapters/quantconnect/lean.py#L625)) | The typed read `history[EODHDUpcomingEarnings]([symbol], start, end, Resolution.DAILY)`, filtered to the subscribed securities in Python, with endpoints converted to the algorithm's zone | Nothing; the first session of a node with a long earnings lookback drops from minutes to seconds |

### 4.2 Keep, because it carries Sugarloaf's semantics

- Neutral `Bar`, `Quote`, `IndicatorSnapshot`, `FundamentalRecord`, `EarningsRecord` and
  the requirement vocabulary; nodes never see LEAN objects.
- The cutoff from observed bar ends, receipt from algorithm UTC time, admission at `apply`,
  the held facts on data-less slices, and warm-up without decisions
  ([host.py:206](../../sugarloaf/adapters/quantconnect/host.py#L206)).
- Per-requirement readiness, `require_all=False`, `min_lookback`, freshness and the
  declaration guards in `ProviderServices`.
- Feed planning (the finest reader decides; a feed is never made finer in place), universe
  generations and retirement, the one-subscription-per-security rule.
- The corporate-action rule and `DataReset`; the lifecycle-driven withdrawal in the runtime.
- The gateway and engine: reservations, cumulative fills, cancel semantics, refusals.
- The QuantBook causal replay contract (its implementation can batch; its semantics stay).

### 4.3 Add

- **A declared long-running step.** A node (or the deployment) declares that its evaluation
  may exceed the time loop; the host then runs that graph step, and the prefill of newly
  selected members, inside `algorithm.train`. In a backtest the step executes at the start
  of the next time slice, before that slice's `on_data` (verified in D2: requested at 16:00 on 5 January, run before the 16:15 slice with the algorithm clock at 16:00),
  with the same information set for a daily graph; the host records the deferral in the
  batch's receipt time. The budget is the node class's bucket (30 minutes on B2-8, refilled
  5 minutes per real day), so a 248-second refit fits and a 20-minute one does not.
- **A derived-table cache, if still needed.** With the typed read the calendar costs
  seconds, so a cache is no longer required for it. The mechanism stays worth having for
  tables a node builds itself: `launcher.load` offers factories a `stored` writer beside
  the reader, keyed by dataset, span and version, following LEAN's Object Store idiom.
  Streaming the dataset through a subscription is the live-trading shape, and D2b put a
  number on it: LEAN's warm-up delivered all 1.9 million listings of 19 years in 6 s more
  than the bars alone.
- **A columnar read**, `ctx.market.closes(requirement, instruments)` or similar, returning
  arrays aligned on the calendar, for cross-sectional nodes that today rebuild arrays from a
  million `Bar` objects each session. Optional; the `Bar` read stays.
- **Read caching per Context.** `_ContextServices._read` filters the whole window again on
  every call; `ready()` followed by `bars()` is two passes. Caching the filtered tuple per
  (resource key, now, available_at) within the snapshot halves the per-session read cost
  measured in D3 (two passes of 0.21 s over 760,000 bars, unprofiled). This is a change in `sugarloaf/adapters/base.py`, shared by
  the memory adapter.
- **Results to the Object Store.** One JSON file per backtest under the project's prefix,
  with the log carrying a pointer and the summary.

### 4.4 Migration order

1. Adapter-internal, no contract change, each with a native run against the move-size
   probe: the typed earnings read with algorithm-zone endpoints, the admission dedupe, the
   bulk conversion with cached offsets, the read cache in `base.py`, `seed_initial_prices`,
   the bar-count daily prefill. Expected effect: the probe's first data callback from
   151 s to under 25 s (the typed read alone takes 142 s of it away), and a session from
   0.6–1.4 s to about 0.2 s.
2. The `stored` writer for node-built tables, and results to the Object Store.
3. The long-running step through `train`, after D2's timing is confirmed in a Sugarloaf run
   with a real refit; document the deferral and the bucket.
4. `indicator_history` warm-up and `SCALED_RAW` rebuilds, with the October lifecycle checks
   rerun.
5. `universe_settings.schedule` for cadenced universes, measured against a daily selection.
6. The research replay: measure the 214 × 4,900 replay in a kernel, then batch admission.

### 4.5 What nodes would see change

Nothing in the Context API changes for existing nodes. The move-size node runs unchanged
through steps 1, 2 and 5. Step 3 is opt-in. Step 4 changes the data raw readers get after a
split (continuous, scaled) and is therefore a documented behaviour change for raw-price
nodes. The columnar read is an addition a cross-sectional node may adopt.

## 5. Measurements

All runs: QuantConnect project 39378262, B2-8 node, LEAN 2.5.0.0.18264. Scripts, manifests
and the committed result files are in [tools/qc_adapter_review/](../../tools/qc_adapter_review/).
Expected costs are stated before each measurement, as the method asked.

### 5.1 D1: plain LEAN, the panel's reads

Run `22bfc237954ecaf0d68b2ac66b590974`, 4–22 January 2021, one task per session;
result file [native_history-22bfc…json](../../tools/qc_adapter_review/results/native_history-22bfc237954ecaf0d68b2ac66b590974.json).
The panel: the 200 most liquid stocks priced at $5 or more with fundamentals, selected
natively at midnight on 4 January 2021 from 8,923 fundamental rows (3,932 eligible), plus
the 14 funds and the VIX. LEAN itself warned at the first history call: "large history
request, estimated at ~5,437,740 data cells across 214 request(s)", and during the
earnings task that "the current time step has been executing for 3.0 minutes and the
algorithm will be stopped if it exceeds 10 minutes".

Expected before measuring: LEAN merges one reader per symbol in C#, so the 756,000 daily
bars should take seconds (the Lab's kernel loads 8,300 stocks over 28 years in 4.5
minutes); the calendar is about 4,800 whole-market files, so minutes; selection, indicator
warm-up and fundamentals should be under a few seconds.

| Task | Measured | Note |
| --- | ---: | --- |
| Native selection of the panel | 3.9 s | Python over 8,923 rows, verified |
| 214 × 4,900 daily bars, one typed call, enumerate only | 5.4 s | 756,082 bars in 4,899 slices |
| Same, every field read in Python | 5.4 s | the six field reads cost nothing measurable |
| Same, plus two exchange-to-UTC conversions per bar | 8.0 s | the adapter's `to_utc` pattern adds 2.6 s per 756,000 bars |
| Same read as a DataFrame | 5.8 s | 756,296 rows; `close.unstack(0)` 0.2 s |
| Same bars by count, `history[TradeBar](symbols, 4900, DAILY)` | 2.4 s | the cheapest shape; LEAN counts sessions itself |
| One typed call per symbol, 214 calls | 3.9 s | the pre-`2a9f6ad` shape is not slow in itself |
| Earnings calendar, 6,800 days, typed `history[EODHDUpcomingEarnings]` | 6.6 s, 0 rows | returned nothing in every year; D2 tests the typed shapes |
| Earnings calendar as yearly DataFrames (the adapter's read) | 374.4 s | 1,901,332 rows, 232,778 kept after reduction; reduction 4.3 s in all |
| `warm_up_indicator` for 214 SMA(252) | 0.46 s | 197 ready (17 securities have under 252 bars) |
| `indicator_history` of one SMA over 4,900 bars | 0.03 s | |
| `security.fundamentals` for 200 securities | 0.11 s | 185 with a sector code |
| `self.fundamentals(symbols)` for 200 | 0.001 s | |
| `history[Split]` and `history[Dividend]`, 214 symbols, 19 years | 2.5 s | 222 splits, 7,814 dividends |

The whole run took 421 s of wall time for 8.5 million data points; 374 s of it was the
earnings calendar. The yearly DataFrame read grows with the file sizes, from 7.0 s for 2002
(31,995 rows) to 46.5 s for 2020 (170,200 rows), about 0.25 ms per row: the DataFrame
conversion of a custom data type, not the adapter's reduction, is the cost. The two
earnings figures the Lab measured through the adapter (69 s for 600 days in 2020, 326 s
for 6,800 days in 2021) match this read alone, so the adapter adds about one per cent.

### 5.2 D3: the adapter, probed with the move-size declarations

Run `4ceff346087229e0619b3aab3224465e` (profiled) and `1a4e11b1f0a4f936b241ca7569778032` (unprofiled), 4–29 January
2021; result files [adapter_probe-4ceff346…json](../../tools/qc_adapter_review/results/adapter_probe-4ceff346087229e0619b3aab3224465e.json)
and [adapter_probe-1a4e11b1…json](../../tools/qc_adapter_review/results/adapter_probe-1a4e11b1f0a4f936b241ca7569778032.json).
The probe node declares the move-size node's requirements (section 4 of
[adapter_probe.py](../../tools/qc_adapter_review/adapter_probe.py)) and reads everything once per
SPY close; it fits nothing, so its evaluation time is the adapter's read cost. The
profiled run wraps the host's constructor and data callback in `cProfile`; profiling
slows Python code by roughly half to double, so the profiled run gives the breakdown and
the unprofiled run gives the wall time.

Expected before measuring, from D1: the members' prefill should cost LEAN's read (about
5 s) plus the adapter's conversion of 760,000 bars; the first evaluation should cost the
earnings read (about 374 s); a steady session should cost whatever the adapter does with
760,000 retained bars per read pass.

| Phase | Profiled | Of which | Unprofiled |
| --- | ---: | --- | ---: |
| Host construction: 15 explicit securities subscribed and prefilled | 4.6 s | LEAN `history` 1.5 s; `_fill_window` 2.7 s (72,545 bars: 145,090 `to_utc` calls 1.1 s, `Bar` construction 1.0 s); subscriptions and initializers 0.3 s | 1.8 s |
| First data callback (4 January, 16:00), in all | 470.3 s | below | 151.4 s |
| of which `apply` → `_sync_resources`: the 200 members' prefill | 25.9 s | LEAN `daily_histories` 6.6 s for 688,000 bars; `_fill_window` 18.9 s (`Bar.__post_init__` 8.4 s, `to_utc` 5.2 s, `aware` 3.4 s) | |
| of which the node's evaluation | 444.2 s | earnings 439.0 s; readiness scan 2.45 s; bar reads 2.45 s; fundamentals 0.37 s | |
| of which the earnings load inside `LeanAPI.earnings` | 438.5 s | pandas `concat` inside LEAN's DataFrame assembly 295.6 s (20 calls); other pandas index work about 100 s; the adapter's generator body 19 s; its own reduction about 4 s | |
| A later session's data callback (16:00) | 5.7–8.0 s | node: readiness 2.4 s, bar reads 2.4 s, fundamentals 0.04 s, earnings 0.24–2.5 s; host and runtime about 0.5 s | 0.6 s in early January, 0.9–1.4 s in late January |
| The 16:15 callback carrying only the VIX bar | 0.01 s | one admission, one account read | |

The whole profiled run took 597 s of wall time for 2.9 million data points. Three things
the profile settles:

- **The earnings cost is LEAN's DataFrame assembly, in pandas, in the algorithm's own
  process.** `history(EODHDUpcomingEarnings, …)` builds one frame per symbol and joins them
  with `pandas.concat` (`Common/Python/PandasConverter.DataFrameGenerator.cs` L164–227, one `PandasData` per symbol joined by `ConcatDataFrames`, `PandasConverter.cs` L185–235);
  for a year with 6,000 reporting companies that is 6,000 frames and a `MultiIndex` union
  chain (230,073 `Index.union` calls, 117,569 `_outer_indexer` calls, 176 s). The 326 s the
  Lab measured, D1's 374 s, this run's 439 s and the unprofiled run's 142 s are the same
  work on the same node class (the spread is noted in the appendix); the adapter's
  reduction is one per cent of it. The daily extension reads show the same shape:
  0.24 s on a quiet day, 2.5 s in late January when more companies list a report.
- **The members' prefill is three parts conversion to one part read.** LEAN delivered
  688,000 bars in 6.6 s; building the neutral `Bar` values took 18.9 s, half of it the two
  `to_utc` conversions and the `aware` checks per bar. Verified; the unprofiled ratio is
  in the right-hand column.
- **A steady session is two full passes over the retained window.** The node's `ready()`
  scan and its `bars()` reads each filter 187 × 4,900 retained bars in Python
  (`_ContextServices._read`), 2.4 s each profiled and 0.21 s each unprofiled; nothing in
  LEAN is touched. The
  admission of the day's 214 bars is milliseconds; the dedupe search is 4,428 comparisons
  per bar, 0.004 s for the VIX slice shown.

LEAN logged, at the first callback, "large history request, estimated at ~5,082,000 data
cells across 200 request(s)", the three-minute time-step warning, and "Request for future
history modified to end now": the adapter passes naive UTC endpoints to the earnings
history call, which LEAN reads in the algorithm's zone (five hours later), so the end is
clamped to now and the start sits five hours late. In a backtest this is harmless, since
the files are daily; it is still a zone mismatch to fix
([lean.py:643](../../sugarloaf/adapters/quantconnect/lean.py#L643)).

### 5.3 D2: LEAN warm-up and `train`

Run `c251b8184ad54bc3be6db9355368ef69`, result file
[native_warmup-c251b818…json](../../tools/qc_adapter_review/results/native_warmup-c251b8184ad54bc3be6db9355368ef69.json).
The 214 securities D1 selected plus the VIX, subscribed daily with `set_warm_up(4900,
Resolution.DAILY)`; LEAN wound the clock back to 12 July 2001.

Expected before measuring: LEAN streams about 755,000 daily bars through the engine, so
tens of seconds at most; the `train` callback should run at a later time step and be
allowed past one minute.

| Measurement | Result |
| --- | --- |
| Warm-up, initialize to `on_warmup_finished` | 10.4 s; 12,538 data callbacks; 754,902 bars |
| `train(fn)` requested inside `on_data` at 16:00 on 5 January | `fn` ran before the next slice (16:15) with the algorithm clock at 16:00; LEAN warned after one minute and let it run; 75 s completed |
| Earnings 2020, `history[EODHDUpcomingEarnings](symbol, start, end)` | 0 rows (and 0 with an explicit daily resolution, and with a trailing span) |
| Earnings 2020, `history[EODHDUpcomingEarnings]([symbol], start, end, Resolution.DAILY)` | 164,641 rows in 0.55 s |
| Earnings 2020, `history(EODHDUpcomingEarnings, symbol, start, end)` DataFrame | 170,200 rows in 57.9 s |
| Whole run | 145 s wall time, 1.6 million data points |

The warm-up figure is the one the history incident turned on: LEAN's own indirect history
for exactly this panel and lookback is ten seconds. The typed earnings read works only in
its list form; the single-symbol typed overload returned nothing in every shape tried
(verified twice, cause not established in LEAN). The row counts differ by 3 per cent
between the typed and DataFrame shapes, which the next run examines.

#### D2b: the same with the earnings calendar streamed, and the 19-year typed read

Run `512edc41a254c32ab79cf0c43a575777`, result file
[native_warmup-512edc41…json](../../tools/qc_adapter_review/results/native_warmup-512edc41a254c32ab79cf0c43a575777.json).
The same algorithm with the EODHD calendar subscribed before warm-up, and a fourth
session that reads the whole 6,800-day calendar in the typed list shape a year at a time,
keeping the rows of the panel's securities.

| Measurement | Result |
| --- | --- |
| Warm-up with the calendar subscribed | 16.4 s; 16,384 callbacks; 754,902 bars and 1,899,912 listings delivered |
| Typed read of 6,800 days, `history[EODHDUpcomingEarnings]([symbol], year, stop, Resolution.DAILY)` per year | 9.2 s; 1,850,087 rows, 80,396 of them the panel's; 0.23 s (2002) to 0.87 s (2020) per year |
| Earnings 2020 as a DataFrame, repeated | 44.3 s (57.9 s in D2, 46.5 s in D1) |
| `train` check, repeated | as in D2 |
| Whole run | 147 s wall time, 5.5 million data points |

Streaming the whole calendar through LEAN's warm-up costs six seconds more than the bars
alone, and the typed read of the same span nine seconds against 142–374 s for the
DataFrame shape. The typed rows are 2.7 per cent fewer than the DataFrame's 1,901,332
(D1): a typed slice holds one entry per symbol, so a company listed twice on one day keeps
one entry, where the DataFrame keeps both rows and the adapter's reduction then picks the
earliest report date and first timing. Which of the two the typed slice keeps was not
checked; the migration must compare the kept listings on one year before switching.

### 5.4 Local microbenchmarks

[local_bench.py](../../tools/qc_adapter_review/local_bench.py) times the adapter's own
Python on this machine (CPython 3.13, the tests' fake LEAN boundary, so no Python.NET):
admitting a new daily bar into a 4,900-bar window 0.57 ms, all of it the dedupe scan, so
0.12 s a session for 214 members; one read pass over 4,900 bars 0.6 ms, so 0.26 s for two
passes over 214 members (0.42 s on the B2-8 node in D3); building one neutral `Bar` with
its two time conversions and checks 3.6 µs, 2.7 s per 760,000 bars (about twice that on
the node with real conversions, by the D3 ratio).

### 5.5 Earlier evidence used

- Lab `backtest_check_results.json` on branch `node/move-size-global-2026-11-13`, project
  39251887: four runs stopped at ten minutes before `2a9f6ad`; after it, first-session
  load 28.9 s for the bars and 326.4 s for the earnings calendar (run `477134e1…`), refit
  247.6 s (`68f9ccf3…`), steady sessions 1.6 s.
- [VALIDATION.md](../VALIDATION.md): minute against daily feeds for a 250-stock universe
  (16.3 million data points in 249 s against 0.49 million in 29 s), the thirteen-year run
  (48 minutes against 33 for the plain algorithm), the consolidator feed and order-type
  probes of October.

## 6. Decisions asked of Bobby

1. Approve the adapter changes of migration step 1 (Sugarloaf Core).
2. Choose the long-running step design: `train`-backed deferral to the next time slice as
   proposed, or keep the ten-minute budget as a design constraint on nodes.
3. Confirm the earnings strategy: the typed read as the first step, with the Object Store
   cache and streaming kept as options rather than built now.
4. Decide whether the node-visible additions (columnar read, `stored` writer) are in scope.
5. Accept `SCALED_RAW` rebuilds as a behaviour change for raw-price readers.
6. Confirm the node class to measure against; the training bucket doubles from B2-8 to
   B4-12.

## Appendix A. Verified and inferred

| Statement | Status | Evidence |
| --- | --- | --- |
| LEAN reads 214 × 4,900 daily bars in one typed call in about 5 s, by count in 2.4 s, per symbol in 3.9 s, as a DataFrame in 5.8 s | Verified | D1 |
| Reading the six fields of every bar in Python costs nothing measurable; two Python.NET time conversions per bar cost 2.6 s per 756,000 bars | Verified | D1 |
| The earnings calendar's cost is LEAN's DataFrame conversion of about 4,800 whole-market files, 374 s for 19 years; the adapter's pandas reduction is 4.3 s | Verified | D1, Lab runs `477134e1…` and `139ecd2c…` |
| The typed `history[EODHDUpcomingEarnings]` read returns no rows for a single symbol and date range | Verified in two runs, cause not established | D1, D2 |
| Native selection of the 200-stock panel takes 3.9 s over 8,923 rows; `warm_up_indicator` for 214 indicators 0.46 s; split and dividend history for 214 symbols over 19 years 2.5 s | Verified | D1 |
| Through the adapter, the panel's first data callback is 151 s unprofiled: 142 s the earnings DataFrame read, about 9 s the members' prefill (LEAN 6.6 s, conversion 18.9 s profiled); a later session 0.6–1.4 s, of which two 0.21 s read passes and a 0.05–0.93 s daily calendar extension | Verified | D3 (`4ceff346…` profiled, `1a4e11b1…` unprofiled) |
| The 19-year typed earnings read takes 9.2 s and returns 2.7 per cent fewer rows than the DataFrame (1,850,087 against 1,901,332), consistent with one entry per symbol per day in the typed dictionaries; which duplicate survives is not established | Verified count; cause inferred | D2b, D1 |
| The earnings DataFrame read of the same span cost 374 s (D1), 326 s (Lab), 142 s (D3 unprofiled) and 439 s (D3 profiled) on the same node class | Verified spread; cause not established | D1, D3, Lab |
| LEAN's own warm-up streams 4,900 daily bars for 215 securities in 10.4 s; a `train` callback requested in `on_data` runs at the start of the next time step with the clock set to its request time, and LEAN requests extra time after one minute | Verified | D2 |
| The typed earnings read returns rows only in the list-of-symbols form with an explicit daily resolution; it is about a hundred times faster than the DataFrame read (0.55 s against 57.9 s for 2020) | Verified; the 19-year read took 9.2 s in D2b | D2, D2b |
| One time loop may take ten minutes; `train` and scheduled events extend it from a leaky bucket sized by node class; the bucket refills in real time | Verified in the docs and source | Training Models; Resources › Training Quotas; `AlgorithmTimeLimitManager.cs`, `TimeMonitor.cs`, `IsolatorLimitResultProvider.cs` |
| A `train` callback requested in `on_data` runs at the start of a later time step, before that step's `on_data` | Verified in source and in D2 | `ScheduleManager.cs` L228, `BacktestingRealTimeHandler.cs` L100–149; D2 |
| `_admit_bar` searches the whole window for an equal stamp on every admission | Verified by reading | [services.py:937](../../sugarloaf/adapters/quantconnect/services.py#L937) |
| `_ContextServices._read` filters the whole window on every read; `ready()` then `bars()` is two passes | Verified by reading | [base.py:678](../../sugarloaf/adapters/base.py#L678) |
| Universe members selected on a cadence boundary stay unchanged on other days, where LEAN still loads the day's fundamentals and calls the selector | Verified by reading; the cost of the daily call not measured | [services.py:261](../../sugarloaf/adapters/quantconnect/services.py#L261); Universes › Settings |
| `settings.seed_initial_prices` seeds new securities as the adapter's initializer does | Verified in the docs; not run | Initialization |
| With `RAW` data LEAN's idiom after a split is a reset and a `SCALED_RAW` warm-up; the adapter rebuilds with raw history | Verified in the docs and code | Corporate Actions; [services.py:391](../../sugarloaf/adapters/quantconnect/services.py#L391) |
| The research replay costs about a million per-bar Python operations for the panel | Inferred from code; not measured | [research.py:171](../../sugarloaf/adapters/quantconnect/research.py#L171) |
| 214 minute feeds would cost about 250,000 per-bar Python.NET conversions a day | Inferred from the call count; not run | [services.py:561](../../sugarloaf/adapters/quantconnect/services.py#L561) |
| `debug` messages are documented at 200 characters; the adapter's result records exceed that and have arrived intact | Verified in the docs and in retained logs; not a guarantee | Logging; `artifacts/ema` logs |
| The adapter's earnings stamp equals the time LEAN would emit the listing | Verified by reading the data source | `EODHDUpcomingEarnings.cs` (`Time = date − 1`, `EndTime = date`) |
