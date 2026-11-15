# Validation and limits

## Redesign of 14 November 2026

The current code (declarations in `sugarloaf.core.inputs`, `GraphRunner`, `QuantConnectAdapter`
and `MemoryAdapter` as mappings, forecast runs saved to the Object Store) has local test
coverage and the native checks below. On branch `redesign/push-model-2026-11-14`,
`uv run --cache-dir .uv-cache pytest` passes 150 tests and `ruff check .` passes.

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
any other; the five checks below take a few minutes in total.

The script runs each manifest through `run_backtest` (upload, compile, backtest, status and
logs, with evidence under `artifacts/<manifest stem>/`), one backtest after another; the five
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

Project `37377184`, LEAN as deployed by QuantConnect on that day, the adapter with
`from_history` and `listed_tickers` (branch `check/adapter-coverage-2026-11-15`) plus the
manifests' parameters; the Lab runs below used `2ee2c5d`. Each run's source hash is in
`artifacts/native_checks/` and `artifacts/<manifest stem>/` (git-ignored). Result records are
in the Object Store under `sugarloaf/results/37377184/<backtest id>/result.json`; the forecast
run also saves `forecasts.json`.

| Check | Backtest | Observed |
| --- | --- | --- |
| EMA example, trading ([ema.toml](../deployments/quantconnect/ema.toml), January 6–10, 2025, USD 100,000, 5-minute bars) | `d76245fc607a7ba88e6c3893aa9c6fa6` | No runtime errors, no diagnostics, 1,560 steps (four sessions of minute slices; the market is closed on 9 January). 6 orders, USD 6.00 fees, end equity USD 99,652.57, net profit -0.347%. |
| EMA forecast run ([ema_forecasts.toml](../deployments/quantconnect/ema_forecasts.toml), same window) | `9cccde8becf73ebf3a180a82a20358c1` | No orders, no diagnostics, 1,560 steps; 312 views archived (`SUGARLOAF_RESULT`), one per 5-minute bar of the four sessions (4 × 78). |
| Index check ([index_check.toml](../deployments/quantconnect/index_check.toml), March 24 to April 17, 2025) | `0fb8d6c2b1c930c4f2dca0d3fb4513a8` | No orders, no diagnostics, 38 steps (one SPY slice and one VIX slice per session, 19 sessions). The check reads daily feeds without consolidation, which `from_history` does not touch. VIX daily bars run 13:30–20:15 UTC and arrive in their own slice after SPY's 20:00 close, so at the SPY close the VIX window holds the previous session's bar: 0 of 19 same-session; highest VIX close 52.24 on 8 April 2025 (result record of `4b9f5a3d9a149d06ef95df2d7ac6def1`, same feeds and adapter path). |

`from_history` keeps a consolidated bar built from history bars alone out of the next
arrival (`QuantConnectAdapter._track`, handler `completed`; [adapter
README](../sugarloaf/adapters/quantconnect/README.md#windows-and-warm-up)). The history fill in
`_fill_group` feeds the window's live consolidator the bars ending earlier than the fill's cutoff;
LEAN emits the last period it holds from them at the next bar or scan, outside the fill. A bar
ending no later than the cutoff primes the window only. Without the rule, the forecast run
archives 313 views and the trading run ends at USD 99,665.11 (source at `7105f38`, backtests
`382dd5c2cf7db09c0bdc4ea3d47371a2` and `df71573d7e9f939d128ca43f74b2cefd`): one 5-minute bar
more, which the coverage check below identifies as a period ending earlier than the start of the
warm-up replay.

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

Not yet exercised natively: a refused ticket. The [coverage check](#coverage-check) exercises
the other paths no check above reaches.

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
| Adapter with the rules and `from_history` (source hash `736832f4…b61c7`) | `fbb5498a55efd76c5c82a6a7dca276f9` | 60 / 60 | 43 / 43 | 60 / 60 | 43 / 43 | 0 of 34, for each window and indicator |

`on_securities_changed` performs all four setup fills (the SPY and QQQ
windows and their EMAs) at 05:00 UTC on 22 November 2024, the start date minus 45 days, with
the cutoff at that clock. No evaluation, of 30 during warm-up and 4 after
it, sees a repeated end time or a window or indicator value ending after `ctx.time`. The
replay delivers 28 SPY and 28 QQQ bars to the node in `data.bars` with `ctx.warming_up` set:
the QQQ session the live consolidator holds from history at the fill ends earlier than the
fill's cutoff, primes the window and stays out of the arrival (`from_history`),
and the first bar after warm-up (the session ending 21:00 UTC on 6 January 2025, for SPY and
QQQ) heads its window. Each EMA's window holds 43 values at the end of the run: the values
from `warm_up_indicator`, which reads the indicator's own warm-up period of history, and one
per session the replay and the run deliver; its 60 places fill as the run continues. No orders, no diagnostics, no runtime errors,
12,122 steps. The run exercises `set_warm_up(span)`,
`IdentityDataConsolidator[TradeBar]` and `TradeBarConsolidator` feeding
`ExponentialMovingAverage.update` through `subscription_manager.add_consolidator`,
and `warm_up_indicator` at a `Resolution` and at a session interval and `history[TradeBar]`,
both called from `on_securities_changed` during warm-up. The paths in which a consolidator
handler, `on_data`, a read or a scheduled function performs a fill are covered by the unit
tests, not by this run.

The other native checks on the same adapter are in the table of [15 November](#native-checks-15-november-2026).

Local evidence is in `artifacts/native_checks/` and `artifacts/<manifest stem>/` (git-ignored).

### Coverage check

The native check is [coverage_check.py](../deployments/quantconnect/coverage_check.py)
([coverage_check.toml](../deployments/quantconnect/coverage_check.toml), listed in
`native_checks.json`): project `37377184`, July 21 to August 1, 2025, a `WarmUp(span=10 days)`
whose replay starts at 04:00 UTC on 11 July. One node declares every path below. At each
callback it counts, per declaration, the arrivals (records delivered to it) and the distinct
bar end times or record times among them, and per indicator the distinct value end times it
reads and whether it becomes ready. `_report`, which `launcher.finish` calls, saves the counts
in the result record (`sugarloaf/results/37377184/<backtest id>/result.json`, record
`coverage_check`), writes them to the backtest log as one `COVERAGE_CHECK` line, and raises
`AssertionError` naming each declaration with no arrival; the assertion message carries the
same line, because LEAN keeps no log message of the final step when `on_end_of_algorithm`
raises.

Backtest `16e45840de6ff95b3972c566c4450e14` (source hash `22ab5b3e…d578b1`): no runtime
errors, no diagnostics, no orders, 6,284 steps, 2,340 evaluations during warm-up and 3,908
after it. Counts from the `COVERAGE_CHECK` line:

| Declaration | LEAN calls | Arrivals | Distinct times | During warm-up | Observed |
| --- | --- | --- | --- | --- | --- |
| `Bars` SPY minute, window 30 | `add_security`, `RollingWindow[TradeBar]`, `slice.bars` | 6,240 | 6,240 | 2,340 | 16 sessions × 390; first bar ends 13:31 UTC on 11 July |
| `Bars` SPY, 5-minute interval, window 12 | `TradeBarConsolidator(timedelta)` | 1,248 | 1,248 | 468 | 16 sessions × 78; first bar ends 13:35 UTC on 11 July |
| `Quotes` SPY minute, window 5 | `RollingWindow[QuoteBar]`, `slice.quote_bars` | 6,240 | 6,240 | 2,340 | every entry has a bid and an ask |
| `Custom("earnings")` on SPY and AAPL | `add_data(EODHDUpcomingEarnings, "earnings")`, `slice.get` | 8 | 8 | 0 | all AAPL, report date 31 July 2025, `after_close`, first at 04:00 UTC on 24 July; SPY, an ETF, none. AAPL has no other declaration. |
| `ETFConstituentsUniverse` on SPY, count 2, daily | `universe.etf(ticker, settings, selector)` | 2 | 1 | 2 | NVDA and MSFT join at 04:00 UTC on 11 July, in the replay; none leaves |
| `Bars` daily of its members, window 5 | `on_securities_changed`, `history[TradeBar]` | 32 | 16 | 12 | 2 members × 16 sessions |
| `FundamentalUniverse`, monthly cadence (three energy stocks by dollar volume) | `FundamentalUniverse.usa(selector, settings)`, `settings.schedule.on(date_rules.month_start())` | 5 | 2 | 3 | three members join at 04:00 UTC on 11 July, in the replay; at 04:00 UTC on 1 August one joins and one leaves; no change between |
| `Bars` daily of its members, window 5 | as above | 48 | 16 | 18 | 3 members × 16 sessions |
| `Schedule` at 10:02, `America/Chicago`, every day | `time_rules.at(10, 2, DateTimeZoneProviders.Tzdb["America/Chicago"])`, `date_rules.every_day()` | 22 | 22 | 10 | every call at 10:02 in Chicago (15:02 UTC); one per calendar day from 11 July to 1 August, weekends included, since the declaration names no calendar |

| Indicator on SPY (window 5) or on members | LEAN class and consolidator | Value updates read | Distinct end times | Ready |
| --- | --- | --- | --- | --- |
| `sma` 10, `rsi` 14, `donchian` 20 at minute | `SimpleMovingAverage`, `RelativeStrengthIndex` (Wilders), `DonchianChannel`; `IdentityDataConsolidator[TradeBar]` | 6,240 each | 6,240 each | yes |
| `sma` 10, `rsi` 14, `donchian` 20 at 5 minutes | the same classes; `TradeBarConsolidator(timedelta)` | 1,249 each | 1,249 each | yes |
| `sma` 3 daily on each `FundamentalUniverse` member | `SimpleMovingAverage`; `IdentityDataConsolidator[TradeBar]`, removed with `remove_consolidator` when the member leaves on 1 August | 52 | 17 | yes |

The 5-minute indicators read one value more than the 5-minute bars: the value
`warm_up_indicator` sets at the fill, which the node reads at the first slice, ahead of the
first completed 5-minute bar; the minute indicators update from that slice's bar ahead of
the node's read. `on_securities_changed` performs every fill: the SPY windows and indicators
and the first members' windows and indicators at 04:00 UTC on 11 July, the joining member's
at 04:00 UTC on 1 August; each fill's clock and callback are in the line.

Two adapter rules come from this check. `listed_tickers` gives `add_custom` the ticker of each
equity a `Custom("earnings")` selection names, and `add_custom` registers its security id, so
`_custom` maps the listing to the declared instrument; without it the instrument maps only
when another declaration subscribes it, and backtest `eb1e14ee388387b1e23e423160388c20`
(source hash `f10d1592…5d180`) delivers no listing and fails the assertion
(`tests/test_qc_earnings.py`). `from_history` keeps the period the live consolidator holds
from a history fill out of the next arrival; without it backtest
`38e1126593aa66106b3182bfc9413758` (source hash `646339e0…3d067b`) delivers 1,249 5-minute
bars, one ending earlier than the start of the replay (`tests/test_qc_window_order.py`).

`GraphRunner.on_scheduled` and `GraphRunner.on_securities_changed` give the node a context
with `warming_up` False during the replay: 10 of the 22 scheduled calls and the membership
changes of 11 July fall in the replay, and the node tells them apart only by `ctx.time`.
`on_scheduled` also calls `_terminal`, which hands a changed `PositionView` to
`adapter.execute`; this forecast run places no order, so the check does not show what LEAN
does with an order during warm-up.

Not exercised by this check: a fill performed by a consolidator handler, `on_data`, a read or
a scheduled function (every first touch here is `on_securities_changed` at 04:00 UTC, outside
any session; `tests/test_qc_window_order.py` covers those paths), a fill of a `Quotes` window
from history (`_fill` fills `Bars` windows only; the replay fills the quote window), a member
leaving the ETF universe, and listings of a universe member under `Custom("earnings")`.

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
