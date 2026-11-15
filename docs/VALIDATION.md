# Validation and limits

## Redesign of 14 November 2026

The current code (declarations in `sugarloaf.core.inputs`, `GraphRunner`, `QuantConnectAdapter`
and `MemoryAdapter` as mappings, forecast runs saved to the Object Store) has local test
coverage and the native checks below. On branch `redesign/push-model-2026-11-14`,
`uv run --cache-dir .uv-cache pytest` passes 150 tests and `ruff check .` passes. The native
evidence further down this page was recorded on the earlier design and applies to the source
snapshots named there.

The QuantConnect adapter has local tests only for its bar-order rules
(`tests/test_qc_window_order.py`); its LEAN calls are verified by the backtests listed below,
run with `tools/native_checks.py`. Each backtest establishes that LEAN accepts the calls it
exercises.

### Native checks

`deployments/quantconnect/native_checks.json` lists the native checks and their expected
statistics. `tools/native_checks.py` runs them on QuantConnect and compares the results. From
the repository root:

```powershell
uv run --cache-dir .uv-cache python tools/native_checks.py deployments/quantconnect/native_checks.json --dry-run
uv run --cache-dir .uv-cache python tools/native_checks.py deployments/quantconnect/native_checks.json
uv run --cache-dir .uv-cache python tools/native_checks.py deployments/quantconnect/native_checks.json --only ema --out artifacts/native_checks/ema-check.json
```

`--dry-run` lists each check's manifest, project, timeout and expectations and contacts no
service. `--only NAME` (repeatable) runs the named checks. `--out` names the output file; the
default is `artifacts/native_checks/<UTC time>.json`, and an existing file is refused so that
earlier results are kept. Each run is a backtest on the account's backtest node, billed as
any other; the four checks below take a few minutes in total.

The script runs each manifest through `run_backtest` (upload, compile, backtest, status and
logs, with evidence under `artifacts/<manifest stem>/`), one backtest after another; the four
manifests share project `37377184`, and each upload replaces the project's files. A run needs
a selected QuantConnect project: the manifest's `project_id`, or the `project_id` that
`sugarloaf create --adapter quantconnect --manifest <manifest>` saved in
`artifacts/<manifest stem>/cloud.json`. A check without one is refused before any backtest
runs. Credentials come from `QC_USER_ID` and `QC_API_TOKEN` or the LEAN CLI's saved login; the
script prints and saves neither, and error messages have both replaced by `[redacted]`.

For each check it compares the backtest's `statistics` with the check's `expected` values:
`Total Orders` exactly, as an integer; a statistic that reads as a number once `$`, `%` and
thousands separators are removed (such as `Total Fees` "$6.00", `End Equity` "99665.11" or
`Net Profit` "-0.335%") within the check's absolute `tolerance` for it, in the statistic's own
unit (dollars, percentage points), zero when none is given; any other statistic as text. A
check with no expected values reports its run as `recorded`. The script prints one line per
check (name, manifest, backtest id, `pass`, `fail`, `recorded` or `error`, and each compared
statistic as observed, expected, difference and tolerance), then a summary. It continues
after a check that fails or raises. The output file holds, per check, the backtest id,
project, compile id, source hash, parameters, comparisons and the full statistics; it is
rewritten after every check, so an interrupted session keeps the checks already run. Exit
status: 0 when every check passes or is recorded, 1 when a check fails or a run raises, 2 when
the check file or the arguments are invalid (nothing has run), 130 on interruption.

The check file is JSON:

```json
{
  "checks": [
    {
      "name": "ema",
      "manifest": "ema.toml",
      "backtest_timeout": 1800,
      "expected": {"Total Orders": 6, "Total Fees": "$6.00", "End Equity": "99665.11"},
      "tolerance": {"End Equity": 50},
      "recorded_backtest": "df71573d7e9f939d128ca43f74b2cefd"
    }
  ]
}
```

`manifest` is a path relative to the check file's folder and must lie inside it. `name`
(default: the manifest's stem) is what `--only` selects and part of the backtest's name.
`backtest_timeout` (seconds, default 1800, at most 21600) bounds the wait for one backtest.
`recorded_backtest` names the backtest whose statistics supplied the expectations, and
`description` is a note; the script reports both and reads neither from QuantConnect. Any
other key, a tolerance on `Total Orders`, a tolerance without an expectation, a negative
tolerance or a duplicate name makes the file invalid. The expectations in the repository's
file are the results of the 15 November runs below, with a tolerance of USD 50 on end equity.

### Native checks, 15 November 2026

Project `37377184`, LEAN as deployed by QuantConnect on that day, source at commit `7105f38`
(the indicator change below) plus the manifests' parameters; the Lab runs below used `2ee2c5d`. Result records are in the Object
Store under `sugarloaf/results/37377184/<backtest id>/result.json`; the forecast run also
saved `forecasts.json` (202,138 bytes, 313 views). Local evidence (bundles, status, logs) is in
the git-ignored `artifacts/<manifest>/` folders.

| Check | Backtest | Observed |
| --- | --- | --- |
| EMA example, trading ([ema.toml](../deployments/quantconnect/ema.toml), January 6–10, 2025, USD 100,000, 5-minute bars) | `df71573d7e9f939d128ca43f74b2cefd` | No runtime errors, no diagnostics, 1,560 steps (four sessions of minute slices; the market was closed on 9 January). 6 orders of 33 or 34 shares, USD 6.00 fees, end equity USD 99,665.11. The record of the earlier design was 6 orders, USD 6.00 fees, USD 99,652.57; its orders are no longer retrievable from QuantConnect, so the USD 12.54 difference is not attributed. The sizing rule is the same; the EMA warm-up is now LEAN's `warm_up_indicator`. |
| EMA forecast run ([ema_forecasts.toml](../deployments/quantconnect/ema_forecasts.toml), same window) | `382dd5c2cf7db09c0bdc4ea3d47371a2` | No orders, no diagnostics; 313 views archived (one per completed 5-minute bar, from 14:31 UTC on 6 January to 21:00 UTC on 10 January), decoded locally with `decode_archive` as `ScoreView` values with calibration `ema-spread:20:50:300.0:raw:v1` and one-day horizons. |
| Index check ([index_check.toml](../deployments/quantconnect/index_check.toml), March 24 to April 17, 2025) | `4b9f5a3d9a149d06ef95df2d7ac6def1` | 19 evaluations, 38 steps (one SPY slice and one VIX slice per session), no diagnostics. VIX daily bars run 13:30–20:15 UTC and arrive in their own slice after SPY's 20:00 close, so at the SPY close the VIX window holds the previous session's bar: 0 of 19 same-session, the same as the record of 11 November below. Highest VIX close 52.24 on 8 April 2025, as recorded. |

What the first attempt found: every run stopped in `initialize` because LEAN's Python
indicator helpers (`ema`, `sma`, `rsi`, `dch`) bind only a `Resolution` for their cadence and
refuse a `timedelta`. Indicators on a consolidated interval are now built by class,
registered with `register_indicator(symbol, indicator, consolidator)` and warmed with
`warm_up_indicator(symbol, indicator, interval)`; the EMA runs above exercise that path.

Calls these runs exercised and LEAN accepted: `add_security_initializer`,
`settings.automatic_indicator_warm_up`, `settings.daily_precise_end_time`,
`settings.seed_initial_prices`, `Symbol.create` for an equity and an index, `add_security`
with the `fill_forward`, `extended_market_hours` and `data_normalization_mode` keywords,
`RollingWindow[TradeBar]`, `TradeBarConsolidator(timedelta)` with
`subscription_manager.add_consolidator`, `ExponentialMovingAverage(name, period)` with
`register_indicator` and `warm_up_indicator`, `history[TradeBar](symbols, count, resolution,
...)` in `initialize`, `exchange.hours.get_next_market_close`, `market_order(...,
asynchronous=True, tag=..., order_properties=...)`, `transactions.get_open_orders` and
`get_order_ticket`, `object_store.save`, and the `on_data`, `on_order_event` and
`on_end_of_algorithm` callbacks.

Lab runs of the same day, recorded in the Lab repository (branch `port/push-model-2026-11-14`):
the momentum-view forecast run (project `38086843`, backtest `f439134103069902cfda206714196677`,
January 2 to March 28, 2024: a 500-stock `LiquidityUniverse`, members' windows warmed on
joining, 30,268 views archived over 61 sessions, no diagnostics, three minutes from upload to
result) and the short-reversal strategy (project `37695106`, backtest
`2cd3bd4fd968429406f205059daa029a`, January 2 to February 29, 2024: 597 orders and +2.917%
against 598 orders and +2.83% recorded for the earlier design and 587 and +2.86% for the
plain LEAN algorithm). They exercised `FundamentalUniverse.usa` with `UniverseSettings`,
`on_securities_changed` with member warming and `remove_consolidator`, a 253- and 782-bar
daily window on named instruments warmed in `initialize`, `cancel_order`, and the
`market_order` path over a 250-name universe. The strategy's first attempt stopped on a
member LEAN had removed from `securities`; the adapter now skips such a symbol (`2ee2c5d`).

The S-1 node's forecast run (project `38055078`, backtest `d8adb6353f6cccd0a3e9f65d8349df5c`,
January 2 to March 28, 2024: 26,414 views over 61 sessions, 153 steps, no diagnostics) added
`security.fundamentals` and the shortable provider's `fee_rate` and `shortable_quantity` read
through `Custom("fundamentals")` and `Custom("borrow")`, and `schedule.on(date_rules.every_day(symbol),
time_rules.after_market_open(symbol, 1), fn)` firing `on_scheduled` once a session.

Not yet exercised natively: `universe.etf`, `UniverseSettings.schedule.on` (every Lab universe
refreshes daily), `add_data(EODHDUpcomingEarnings)` and `slice.get` for it (no Lab node declares
`Custom("earnings")`), `time_rules.at` with a NodaTime zone, quote bars,
`SimpleMovingAverage`/`RelativeStrengthIndex`/`DonchianChannel`, removal of a member's indicator
consolidator, and a refused ticket.

### Windows and indicators under warm-up

A window or indicator holds each bar period once, newest first, and no value ending after the
clock ([adapter README](../sugarloaf/adapters/quantconnect/README.md#windows-and-warm-up)).
The setup's history fills and `warm_up_indicator` calls are performed on first touch, by
whichever path reaches the window or indicator first after `initialize` (its consolidator
handler, `on_data`, a read, `on_securities_changed` or a scheduled function), with the clock
at or after the start of the warm-up replay; a window's fill takes the bars ending before the
bar or clock in hand (`fill_before`, `touch_window`). `add_in_order` adds a bar to a window
only when it ends after the window's newest bar; `update_in_order` feeds a bar to an indicator
only when it ends after `indicator.current.end_time`. An indicator's window is sized before it
is warmed. `tests/test_qc_window_order.py` tests these rules without LEAN, including a
consolidator handler that fires before the fill and leaves the window complete and in order.

The native check is [warm_up_check.py](../deployments/quantconnect/warm_up_check.py)
([warm_up_check.toml](../deployments/quantconnect/warm_up_check.toml), listed in
`native_checks.json`): project `37377184`, January 6–10, 2025, a `WarmUp(span=45 days)`, a
60-bar daily window and a 60-value `ema(10)` on SPY (subscribed daily) and on QQQ (subscribed
at minute, consolidated to the exchange session). At every evaluation the node counts each
window's bars and each indicator's values beside the distinct end times among them, and
whether the newest end time is later than `ctx.time`, and the adapter records the callback
that performs each fill; the report saves the counts in the
result record (`sugarloaf/results/37377184/<backtest id>/result.json`, record
`warm_up_check`) and writes them to the backtest log as `WARM_UP_CHECK`.

| Source | Backtest | SPY window: bars / distinct | SPY indicator: values / distinct | QQQ window | QQQ indicator | Evaluations with a value after `ctx.time` |
| --- | --- | --- | --- | --- | --- | --- |
| Adapter at `54a25e2` | `9663916e6460a28d772f9281f1dd88f0` | 60 / 32 | 7 / 6 | 60 / 60 | 6 / 6 | not counted |
| Adapter with the rules (source hash `967c7e44…dee40`) | `67b1cf1cea103a4bb24a49610ea3b48e` | 60 / 60 | 43 / 43 | 60 / 60 | 43 / 43 | 0 of 35, for each window and indicator |

At `54a25e2` the history fill in `initialize` ends at the start date, the warm-up replay
delivers 28 SPY sessions the window already holds, and the window holds each of them twice;
34 evaluations see a repeated SPY bar and 5 a repeated SPY indicator value. LEAN's
helper-built `ema` accepts the replayed bar for the session it read last from history, a new
object with the same end time, and updates twice on it.

With the rules, `on_securities_changed` performs all four setup fills (the SPY and QQQ
windows and their EMAs) at 05:00 UTC on 22 November 2024, the start date minus 45 days, with
the cutoff at that clock. No evaluation, of 31 during warm-up and 4 after
it, sees a repeated end time or a window or indicator value ending after `ctx.time`. The
replay delivers 28 SPY and 29 QQQ bars to the node in `data.bars` with `ctx.warming_up` set,
and the first bar after warm-up (the session ending 21:00 UTC on 6 January 2025, for SPY and
QQQ) heads its window. Each EMA's window holds 43 values at the end of the run: the values
from `warm_up_indicator`, which reads the indicator's own warm-up period of history, and one
per session the replay and the run deliver; its 60 places fill as the run continues. Both runs: no orders, no diagnostics, no runtime errors,
12,122 steps. The run with the rules exercises `set_warm_up(span)`,
`IdentityDataConsolidator[TradeBar]` and `TradeBarConsolidator` feeding
`ExponentialMovingAverage.update` through `subscription_manager.add_consolidator`,
and `warm_up_indicator` at a `Resolution` and at a session interval and `history[TradeBar]`,
both called from `on_securities_changed` during warm-up. The paths in which a consolidator
handler, `on_data`, a read or a scheduled function performs a fill are covered by the unit
tests, not by this run.

The other native checks on the adapter with the rules match their expectations, with no
runtime errors and no diagnostics:

| Check | Backtest | Observed |
| --- | --- | --- |
| EMA example, trading | `d44dfa73f0de02e3cc259d05051e341d` | pass: 6 orders, USD 6.00 fees, end equity USD 99,665.11, 1,560 steps, as recorded |
| EMA forecast run | `676d83e4d4662ea6e0a6843221af3b44` | pass: no orders, 313 views archived, 1,560 steps, as recorded |
| Index check | `c0daed0d9e5cf46c07bcbbe374208fae` | pass: no orders, 38 steps, as recorded |

Local evidence is in `artifacts/native_checks/` and `artifacts/<manifest stem>/` (git-ignored).

### Capability limits

- QuantConnect data: US equities (ETFs included) and US indices. Other asset classes raise
  `CapabilityError`.
- Orders: market, market-on-open and market-on-close, day or good-till-cancelled, sized by
  `order_quantity`. Holdings, fills, partial fills and cancellations are LEAN's; the runner
  keeps the adapter's order records and LEAN's order feedback for the result file.
- Native indicators: `sma`, `ema`, `rsi`, `donchian`. The memory adapter computes SMA, EMA,
  Wilder's RSI and Donchian in Python from the bars it was handed; LEAN computes its own in a
  backtest, and the two are not compared.
- A node runs when its declared data arrives, when an upstream output changes or when its
  schedule fires.
- `ctx.history` reads at the feed resolution; a request with an `interval` raises
  `CapabilityError` on QuantConnect.
- No restart or recovery of runner state; no live-trading evidence.
- The memory adapter fills every order at the last price given, charges no fee or slippage,
  and has no data it was not handed.
- Overlapping forecast revisions are dependent observations; evaluation reports no
  independent-sample inference.

## Record of the design before 14 November 2026

The sections below record checks of the design that preceded the redesign. Class, method and
setting names in them refer to the code of that date; each run applies to the source
snapshot it names.

### Local verification

Local regression coverage includes immutable View identities, graph
wiring, late/equal-time data, expiry/lifecycle invalidation, schedules, resource
ownership, native-boundary prefill/consolidation, causal replay, outcome alignment,
all predictive scoring families, native-indicator signal nodes, simple aggregation/position conversion, explicit
deployment bundles, and order/fill/cancel flows.
Tests also cover structural custom-provider replay, typed split/dividend
admission and resource reset, immutable deployment source/result evidence,
historical run lookup, submission failures, and complete log pagination. Current
test counts and check outcomes belong with each change's verification record.
The public EMA notebook's code cells parse; its supplied outputs are empty.
The manifest-driven public run interface and QC-local cloud tooling have local
regressions and native acceptance for the EMA deployment and research notebook
described below. Cloud evidence applies to their saved source snapshots.

Current source is locally tested. Cloud evidence applies
to the recorded source manifest below; it does not certify other source snapshots.
The EMA QuantBook replay runs in the native research kernel. Local adapter tests
use injected API boundaries; capabilities outside the observed native paths still
need platform acceptance.

### Cloud evidence

Project `37377184`, January 6-10, 2025, starting equity USD100,000. The EMA run
completed without runtime errors. LEAN 2.5.0.0.18155; Python 3.11.14; NumPy 1.26.4;
SciPy 1.13.1. These are integration checks, not evidence of predictive value.

| Strategy | Backtest ID | Orders | Fees | End equity |
| --- | --- | ---: | ---: | ---: |
| Native EMA, SPY, 5-minute source | `7e6db1d6422d2f17f4f11d1842eb304d` | 6 | $6.00 | $99,652.57 |

The EMA run processed 1,560 data batches and recorded six complete cumulative-fill
updates. It exercises native subscription, consolidation, indicator prefill,
graph evaluation, market submission, ticket feedback and reporting.
Source manifest SHA-256:
`6f2003e9694b0af1e9e8ee84192531ed7b1ae0e73d841e9bab1c189d1c64fb36`.
Sources, compile/submission identity, results, logs and `acceptance.json` are
retained under `artifacts/ema/`.

The exact bundled EMA research notebook completed all four code cells in the same
project's native QuantBook kernel. January 6-18, 2025 replay issued nine forecasts
with no readiness diagnostics. Outcome evaluation scored six and retained three
unavailable exact anchors; none were issued after their target end. The output
notebook is `artifacts/ema/native-research/ema_research.ipynb`. It covers daily
native EMA replay, provenance and outcome evaluation, not corporate-action
acceptance or cross-provider numerical parity.
Public runs use ignored per-manifest artifact directories; each result retains
its exact source/parameter/compile identity.
See [the guide](GUIDE.md#4-deploy-and-inspect) for retrieval and inspection.

### Native acceptance, 9 October 2026

Run on LEAN 2.5.0.0.18178 after the audit changes. Each row answers one
integration question; none says anything about profitability. All runs completed
without runtime errors.

The check graphs are deliberately untracked scaffolding in
`artifacts/qc-acceptance/` (project `37597459`). Every run's exact uploaded
source, including those graphs, is in its bundle snapshot under
`artifacts/qc-acceptance/artifacts/acceptance/`. That directory is git-ignored:
it is evidence on this machine, not a backup.

| Question | Backtest | Observed |
| --- | --- | --- |
| Do session-count horizons follow the exchange calendar, and can a Friday decision trade? | `a09b757e4198fbbcbe46061614c47bc3`, repeated on later source as `ccf76792f3a78eb7c898ed64453efaa3` | Daily SPY bars complete at 16:00 New York. Asked at exactly that close, `session_close` returned the next session's close: Wednesday 8 January → Friday 10 (market closed on the 9th), Friday 10 → Monday 13, Friday 17 → Tuesday 21 (holiday on the 20th). Each 16:00 decision was refused as session-closed and filled at 09:31 on the next session, including Monday after a Friday decision. |
| Does `session_open` return regular opens on the exchange calendar? | `f00603382d4e2b8d1caa8c74c5825f12` (Lab project 38086843, momentum view native check, 2022-11-01 to 2023-06-30) | Asked at each 16:00 New York close, `session_open(SPY, now, 1)` and `(…, 22)` returned 09:30 New York (13:30 UTC) opens only, never pre-market. Friday → Monday (65.5 hours); Friday 26 May 2023 → Tuesday 30 May (Memorial Day skipped, 89.5 hours). Every interval ending inside the window spanned 21 sessions, including Juneteenth; the one from 2 June ends 6 July, counting the 3 July early close and skipping 4 July. Opens in standard time (14:30 UTC) were not exercised. |
| Is shorting permitted under LEAN's default shortable provider? | same runs | The target alternated +30% and −30%. Every short entry filled; no borrow rejection. |
| Does the rebalance band stop drift re-trading? | `de6544d12f1e9191c440fa2c82d90f0c` (band 0) and `a5a5e871104671d47ed44e61e21f1c67` (band 0.25%) | A constant 50% SPY weight on USD10m over four sessions: 953 orders and USD993.89 of fees with no band; 3 orders and USD43.89 with it. The two maintenance orders were 43 and 44 shares, about 0.25% of NAV. |
| Does a corporate action touch only the affected part of the graph? | `bd69f53bdb7d6b22315eebc2caf5b0d7` (and `821fa9dda733cb14178f1bae63edc2cb` without the relay) | June 2024, three independent readers. NVDA's split re-evaluated only the raw NVDA reader. NVDA's and SPY's dividends re-evaluated nothing (raw prices). QQQ's dividend re-evaluated only the adjusted QQQ reader. A relay downstream of the SPY position producer ran exactly once per upstream output, so that target was never withdrawn, and its single order was undisturbed. |
| Can a node work with a partly ready universe? | `5d90c2881a8229085e63ee60ee63cdbb` | A 60-name native liquidity universe with a 150-bar daily lookback, 9–13 June 2025. The `require_all=False` node evaluated on all five days with 58 of 60 members ready (57 on the last day); the unready ones were CRWV, CRCL and CHYM, all recently listed. Its strict twin was withdrawn every day with `Insufficient history`. |
| Does the public EMA example still behave the same? | `8422af7a72778f27074ce931d3235d5b` (project `37377184`) | 6 orders, USD6.00 fees, end equity USD99,652.57: identical to the 4 October run above. |

The research notebook was re-executed in the native QuantBook kernel with a
one-session horizon: nine forecasts issued and **nine scored**, where the
calendar-day horizon had left three unscorable. Output:
`artifacts/ema/native-research/2026-10-09/ema_research.ipynb`.

What these runs found, beyond confirming the changes:

- **Queued facts were being spent on data-less callbacks (fixed).** In run
  `172974f9ada6c6ac6fc3cd503c755ac2` the universe-triggered nodes were withdrawn
  with `Universe unavailable` on two of five days. LEAN had called `OnData` with a
  slice carrying no bars; the host delivered the queued universe event with the
  graph clock still at the previous close, so that morning's selection looked like
  the future and the one trigger for it was consumed. The host now holds
  callback-queued facts until a slice carries data. The repeat run above evaluated
  on all five days.
- **Adjusted prices in a backtest are already fully adjusted.** The adjusted QQQ
  window was identical before and after its ex-dividend date: LEAN applies the
  whole factor file, including later dividends, from the start. The rule these
  runs exercised rebuilt that window and withdrew its reader anyway. It has since
  moved into the QuantConnect adapter, which then rebuilt nothing on a dividend
  (see the parity section below).
- **A node re-evaluated by a lifecycle event emits nothing until its next bar.**
  The event arrives before the open while the graph clock is still at the prior
  close, so the new View's information cutoff predates the event and is rejected.
  This is the existing conservative rule, now applied to the affected node only.
  Since the adapter change it follows a split, not a dividend.
- **Small accounts still make single-lot corrections.** With USD100,000 in SPY,
  one share is 0.6% of NAV, more than the 0.25% band, and the session runs made
  four one-share trades in two weeks as the exact target crossed whole shares.
  `min_trade_value` or a wider band suppresses them.
- **A stale research session answers 404.** `research/read` returned a session
  whose server no longer existed. The notebook runner now checks the server and
  requests a fresh session in that case.

Not covered by these runs: partial fills, cancellations and cancel rejections;
quotes; ETF-constituent universes; delistings and symbol changes; live data.
The session, band and lifecycle runs predate the host fix; only comments changed
in the engine after the final session run.

### Native feeds, order types and feedback order, 11–12 October 2026

Run on LEAN 2.5.0.0.18178 against the working tree on `403edf9`, after the
adapter began subscribing each security at the coarsest native resolution its
readers need and the engine stopped deciding when an order may be sent.

**What LEAN does** (plain algorithm, no Sugarloaf code; project `37597459`,
backtest `dfe6a1846c154ee680ff758bac880fe2`, source in
`artifacts/qc-acceptance/probe/`):

| Question | Observed |
| --- | --- |
| When does a daily bar arrive? | At 16:00 New York with `daily_precise_end_time` at its default, and an `IdentityDataConsolidator` on a daily subscription emits it then |
| Which subscription feeds a consolidator when a security has two? | The first one added, whatever its resolution: daily-first received daily bars, minute-first received minute bars |
| What happens to a market order sent while the exchange is closed? | It becomes a market-on-open order and fills at the next opening price, from a daily bar at 16:00 and from a minute security at 16:00:00 alike |
| When is a market-on-close order accepted? | At 15:40 it filled at the close. At 15:55 the ticket came back invalid, with order ID −10 and no order event |
| When is a market-on-open order accepted? | On a daily security at 09:32 it was accepted and filled at the next day's open. On a minute security during the session it came back invalid |
| Does a daily subscription with fill-forward add data? | Yes: it puts a fill-forward bar in every minute slice when the algorithm also has a minute subscription |

**A cross-sectional graph on daily bars.** A 250-stock native liquidity universe
with a daily 757-bar lookback, a ranking node and a long-short converter,
January–February 2024, USD 10 million (Sugarloaf Lab's short-reversal replay,
project `37695106`). Each row is one backtest of the same graph.

| Change | Backtest | Observed |
| --- | --- | --- |
| Before: minute feeds | `1a6521a198c57b5acd06c1dfdcc833ef` | 599 orders, +2.62%. 16.3 million data points, 249 seconds |
| Daily feeds, default policy | `b2f4db9eeb1f6a29d278fc5a74d8f4dd` | 0.49 million data points, 29 seconds. 1,560 orders, none filled: each was sent at the close, held by LEAN for the next open, and cancelled at midnight by the engine's five-minute order lifetime, on every night that a corporate-action slice caused a reconciliation |
| No engine cancel timer | `72269996aed86af5a5ef909271ae1a4a` | 961 orders, none filled: each day order expired at the next close, the instant the daily bar carrying its fill price arrived |
| Market-on-open, good till cancelled | `fb4da6661e94af844c84c302df2a01c3` | 533 fills. At one close, 75 further orders: a drift order sent while the previous day's 44 orders were filling made LEAN fill the rest before the call returned; the engine then read holdings that included those fills while their feedback was queued, sent 37 orders reversing them and 37 undoing those. A new target cancelled all 75 in the same instant |
| Feedback applied before each broker read | `01e7f614fee5c54084739ac094c41d2a` | The 75 became 1 (the drift order). Same fills and ending equity |
| Stocks-only universe filter | `2ecef86ce87ef62714562b3f6acfe11f` | 619 orders, +2.61%, 0.51 million data points, 28.5 seconds |
| A split leaves weight targets and rankings standing; orders good till cancelled by default | `8f789a06b7d9f988f8f9f84cb395cfb1` | 598 orders, +2.83%, no runtime diagnostics. The plain daily-bar algorithm made 587 orders and +2.86% on the same window (`148f18fa47294c689c46c2d41e09842e`) |

- **Agreement with plain LEAN.** To 22 February the graph made 261 fills and
  +0.98% (`28c607e2c8714f2d5ce449572f440a8e`); a plain daily-bar algorithm
  applying the same rules made 269 fills and +1.01%
  (`5b6894ac6648f411a3175614fb9a35a5`). On the first trading day 37 of 39 symbols
  were common and 36 had identical quantities. The two differ in how they leave
  funds out of the universe.
- **Thirteen years.** The same graph from January 2014 to September 2026, on
  the final source (`1e48b54acf52fd78fa5b7693d4c96e45`): 3,205 sessions, 129,335
  orders, 35.8 million data points, 48 minutes, no runtime errors. It compounded
  at 7.25% a year to USD 24.42 million with a 12.2% largest drawdown; the plain
  daily-bar algorithm at 7.35% to USD 24.70 million with 11.7%, on 133,017
  orders in 33 minutes (`980573343a17cd669e941113e0004b50`).
- **Orders waiting through a split are sized again.** Apple and Tesla split on
  31 August 2020. In the thirteen-year run the orders sent for both at Friday's
  close were cancelled at midnight on the 31st and replaced in the same instant
  (878 and 709 adjusted shares), and the replacements filled at that morning's
  open. The weight target and the rankings stood throughout.
- **A split used to cost a session.** Walmart split on 26 February. Under the
  rule then in force the runtime withdrew every View naming it at midnight, the
  target went with them, and the 56 orders waiting for that morning's open were
  cancelled; the graph issued a new target at that day's close. With minute feeds
  the same gap had lasted one minute. A split now withdraws only Views stated in
  units of the instrument, and in the last row of the table those 56 orders fill.
  Under the earlier rule the thirteen-year run recorded 131 not-ready
  diagnostics (`c66fd34ee534f243113a78683197b744`, 7.26% a year); under this one
  62; splits no longer cause any.
- **A target that expires at the next close is cancelled and filled in one
  instant.** On daily bars LEAN reports yesterday's orders at today's close. If
  the target expires then, the engine asks to cancel the remaining orders after
  the first fill; LEAN has them pending cancellation and fills them in the same
  pass (event sequence submitted, cancel pending, filled, on about 45 orders a
  day). The outcome is the intended one, and it rests on LEAN's ordering.
- **New members have a price.** The security initializer now seeds a new
  security with its last known price; without it a member selected in the
  morning could not be valued at that day's close.
- **A security can be listed twice in a day's selection data.** A run from 2014
  stopped on 6 February 2014 with one stock twice in a ranking
  (`c22f4aa9b27f2a21c551a5c4f75077e0`). Selection now returns each identity once,
  and the first quarter of 2014 completes (`1b9628258b7eaa4228bb1c0d92718b4f`).

Not covered: hourly feeds, quotes on a coarser feed, a feed made finer after its
security was subscribed (the adapter refuses it), a market-on-close policy inside
a graph, and a platform refusal reaching the engine from a live ticket. The
refusal path, the feed planning and the order-type translation have local tests
only.

### Parity with plain QuantConnect algorithms

Five simple rules are each run twice in QC Cloud: once as a native `QCAlgorithm`
with no Sugarloaf code, once as a Sugarloaf graph. All five pairs produce
identical orders (fill time, symbol, quantity and fill price) and identical
ending equity. They were first run on 9 October 2026 and rerun on 12 October
2026 after the feed and order changes above, with the same order counts.

Two things changed on the native side for the rerun, because the graph no longer
waits for the exchange to open before sending an order. The daily rule now
subscribes daily bars and sends its market order at the close, which LEAN holds
for the next open; and the five-minute rule sends the order from its 16:00 bar at
16:00. Before, both sides traded those signals at 09:31. With day orders, the
default at the time, the daily graph placed 56 orders and filled none
(`c517d09fe788a304179166e140f9df18`); orders are now good till cancelled by
default.

| Rule | Orders |
| --- | ---: |
| Buy and hold | 1 |
| Three assets rebalanced to fixed weights at 10:00, with a drift band | 15 |
| Daily close above SMA(5): long, else flat (library `SMATrend`), across two SPY ex-dividend days | 23 |
| Five-minute EMA(5) above EMA(20): long, else flat (library `EMASpread`) | 40 |
| Buy at 10:00, sell at 15:30 every session | 18 |

The pairs are an untracked pytest module, `artifacts/qc-parity/test_parity.py`,
run on demand with `python -m pytest artifacts/qc-parity/test_parity.py -v`. They
need QC credentials and about four minutes of cloud time.

Getting the pairs to agree showed how Sugarloaf differs from a naive native
algorithm. Each is intended behaviour that a strategy author needs to know:

- **A wall-clock schedule without a calendar fires at the next open after a
  weekend or holiday.** `Schedule(at=time(10, 0))` occurs every calendar day and a
  missed occurrence fires at the next data tick. Use `calendar=instrument` for
  "each session".
- **Each order is sized when it is sent.** In a multi-asset rebalance, later
  orders see NAV after the earlier orders' slippage and fees.
- **Warm-up bars are not decisions.** The first decision comes from the first bar
  completed after the start, never from history used to prepare an indicator.
- **A daily-close signal trades at the next session's opening price.** The order
  is sent at the close and LEAN holds it for the open. Orders are good till
  cancelled by default; a day order sent at the close on daily bars expires
  unfilled.
- **A dividend does not disturb a signal.** The daily pair runs from 3 September
  2024 to 17 January 2025, across SPY's ex-dividend days of 20 September and
  20 December. The 19 September close crosses above the average, so a buy is due
  at the ex-date's open, and both sides make it. Run against the previous commit
  (`4a2907d`), the same pair failed: Sugarloaf withdrew the adjusted-price signal
  on the ex-date and bought one session late, on 23 September. The QuantConnect
  adapter now rebuilds nothing on a dividend, so nothing is withdrawn. A split on
  raw prices is not exercised by the pairs.

The pairs do not cover partial fills, cancellations, shorting, universes or
market-on-close orders.

### Index data, 11 November 2026

Question: does a deployment that declares the VIX as an index, with
`Instrument(vix, "VIX", asset_class="index")`, deliver its daily
bars to a node through the generic `add_security` subscription?

Run: [`deployments/quantconnect/index_check.toml`](../deployments/quantconnect/index_check.toml),
project `39250288`, backtest `95a69ac46f6a82704cf7d2139bf80bec`, source hash
`bc92b777…db6b9ed718`, LEAN v2.5.0.0.18250, 24 March to 17 April 2025. One node reads
SPY daily bars (adjusted) and VIX daily bars (raw) at each SPY close and holds
nothing.

Observed:

- 19 evaluations, one per session; no runtime or execution diagnostics, no errors.
- Instrument metadata: SPY equity and tradable; VIX index, and LEAN reports it not
  tradable.
- VIX daily bars are read on every evaluation; highest close 52.24 on 8 April 2025.
- **An index bar closes after the equity session.** LEAN's VIX daily bar runs
  09:30–16:15 New York (13:30–20:15 UTC in the report). At SPY's 16:00 close the
  latest VIX bar a node read was never the same session's (0 of 19); in the rows the
  report lists (the first and last three) it was the previous session's. This is
  causal admission: a bar is readable only after it ends. Reading the same session's
  index value would need an evaluation after the index closes (not tested here).
- Evidence: the run's report is in the backtest's logs on QuantConnect and in the
  local, git-ignored `artifacts/index_check/` folder.

### Capability limits recorded before the redesign

- QC data: US equities (ETFs included) and US indices. Other asset classes are
  refused.
- QC trading: US equities, regular-session day or good-till-cancelled market,
  market-on-open and market-on-close orders; minute, hourly or daily feeds chosen
  from the declared intervals, with native consolidation to coarser ones, and
  synchronous daily universes.
- A graph acts only when data arrives. One that reads only daily bars is evaluated
  once a session, at the close.
- On daily bars in a QuantConnect backtest a `day` order sent at the close never
  fills. The default is `gtc`.
- Native indicators: QC SMA, EMA, RSI and Donchian. No memory formula fallback or
  cross-provider parity promise. Older raw data cannot rewind native indicators.
- QuantBook: explicit instruments, bars and native indicators; split/dividend
  facts replay at effective boundaries, and a split resets raw-price resources
  before reads.
  `price_return` outcome anchors request split-adjusted history; total-return
  anchors request total-return history. Historical receipt is assumed equal to
  observation end. Exact missing outcome anchors stay unavailable. Native numerical
  parity, native corporate-action replay and adjustment-vintage correctness remain
  unverified. Native notebook acceptance covers only the interval and instruments
  above; injected-boundary tests establish the additional local causal contracts.
- The EMA run contains no partial fills or cancellations. Native cancellation
  rejection/retry and partial-fill sequences remain separate acceptance checks.
- No live-deployment/restart recovery or durable state. The memory adapter fills every
  order at the last price given, charges no fee or slippage, and has no data it was not handed.
- Session-count horizons, the rebalance band, scoped lifecycle invalidation,
  partial-universe readiness and default-provider shorting each have one native
  backtest (see the 9 October 2026 acceptance above). That is acceptance of the
  mechanism on one date range, not broad coverage.
- No independent-sample inference is justified for overlapping forecast revisions.
