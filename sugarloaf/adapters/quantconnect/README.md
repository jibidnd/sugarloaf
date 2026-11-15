# QuantConnect adapter

`QuantConnectAdapter` maps each Sugarloaf declaration and read to one LEAN call on a
`QCAlgorithm`, and forwards LEAN's callbacks to `GraphRunner`. LEAN objects stay inside this
package; nodes receive the records in `sugarloaf.core.data`. The full mapping table is in
[adapters](../../../docs/technical/ADAPTERS.md#mapping).

## Module map

| Module | Entry points |
| --- | --- |
| [adapter.py](adapter.py) | `QuantConnectAdapter`: setup (`subscribe`, `add_universe`, `add_custom`, `add_indicator`, `schedule`, `warm_up`), reads (`time`, `window`, `indicator`, `members`, `history`, `calendar`, `portfolio`), `execute`, `save`, callbacks (`on_data`, `on_securities_changed`, `on_order_event`); `Ticker`, `NativeWindow`, `NativeCalendar` |
| [launcher.py](launcher.py) | `load` (calls the deployment factory), `start` (environment, adapter, runner, `SUGARLOAF_RUN`), `finish` (`result.json`, `forecasts.json`, `SUGARLOAF_RESULT`) |
| [main.py](main.py) | `SugarloafAlgorithm`: LEAN's `initialize`, `on_data`, `on_securities_changed`, `on_order_event`, `on_end_of_algorithm`, each a forward |
| [cloud/packaging.py](cloud/packaging.py) | Manifests and offline source bundles |
| [cloud/backtests.py](cloud/backtests.py) | Project, upload, compile, backtest, status and log actions |
| [cloud/client.py](cloud/client.py) | REST client for the QuantConnect API |
| [cloud/kernels.py](cloud/kernels.py), [cloud/notebooks.py](cloud/notebooks.py) | Remote research kernels and notebook execution |

## Call path

`SugarloafAlgorithm.initialize` calls `launcher.load` and `launcher.start`; `start` builds the
adapter and the runner and calls `runner.start()`, which calls the adapter's setup methods.
LEAN then calls `on_data`, `on_securities_changed` and `on_order_event`, which the algorithm
forwards to the adapter and the adapter, after converting LEAN objects, to the runner.
Scheduled functions call `runner.on_scheduled(name)`. `on_end_of_algorithm` calls
`launcher.finish`.

## Windows and warm-up

A window or indicator never holds a value ending after the adapter's clock, and holds each
bar period once, newest first.

**Clock.** The node's clock, `ctx.time`, advances only on event streams: LEAN's callbacks `on_data`,
`on_order_event`, `on_securities_changed` and scheduled functions, as QuantConnect's own
algorithm clock advances only with the [time frontier](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices#02-Time-Frontier)
and its [warm-up](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)
replay. `QuantConnectAdapter.on_data`, `on_order_event`, `on_securities_changed` and the
scheduled function built in `schedule` forward those events to the runner, and
`QuantConnectAdapter.time` reads LEAN's clock. A history request is not an event stream: its
bars prime rolling windows, indicators and node memory and advance nothing. The fill helper
`_fill` (with `_fill_group` and `fill_before`) passes them to `add_in_order` or to a
consolidator whose bars are not marked for the next slice, `add_in_order` stores a bar in a
window, `update_in_order` updates an indicator, and `history` returns bars to the requesting
node only.

**Fill.** `subscribe` creates a named instrument's `RollingWindow`; `add_indicator` builds
its indicators. The window's one `history` call and the indicator's `warm_up_indicator` are
queued in `PendingFills` and performed on first touch: whichever path reaches the window or
indicator first (its consolidator handler, `on_data`, a `ctx.window` or `ctx.indicator`
read, `on_securities_changed` or a scheduled function) performs the pending fill, then
applies its own bar. LEAN runs these only after `initialize`, when it has moved the clock to
the start of the warm-up replay: the start date minus the span of a declared `WarmUp`, or
the start date without one. A window's fill takes the history bars ending before the bar in
hand, or before the clock for a path without a bar (`fill_before`, `touch_window`), so the
bar follows the history in order; `warm_up_indicator` reads history ending at the clock, and
`update_in_order` drops a later bar for a period it read. A consolidated window is filled
through its live consolidator while that holds no data, and otherwise through a consolidator
of its own, which passes only completed periods. The live consolidator emits the period
it holds from history after the fill, when the next bar or LEAN's scan closes it; a bar
ending no later than the fill's cutoff (`from_history`) primes the window and is not carried
in the next arrival. A member's window and indicators are filled
in `on_securities_changed` when it joins, at that callback's clock. The replay then delivers
the bars from the fill's end up to the start date, in order.

**Window rule.** Every bar reaches a window through `add_in_order`: it is added only when
its end time is later than that of the window's newest bar (`window[0]`).

- The first bar after warm-up ends after the last warm-up bar and is added.
- A late bar, one that arrives after a bar ending later than it, stays out of the window;
  the arrival carries it in `data.bars` as LEAN delivered it. A node that must tell such a
  bar apart compares `data.bars[i].end` with `ctx.window(...)[0].end`.

**Indicator rule.** Each indicator is built by class, its `window.size` set to the declared
window, warmed with `warm_up_indicator`, and fed by a consolidator of its period (an identity
consolidator at the security's own resolution) through `update_in_order`: a bar updates it
only when it ends after `indicator.current.end_time`. LEAN's indicators drop an input ending
before the previous one but accept a different bar object ending at the same time; the rule
leaves such a bar out. The window keeps the warmed values, up to its size.

`add_in_order`, `fill_before`, `from_history`, `touch_window`, `PendingFills` and
`update_in_order` are module-level, tested without LEAN in
[tests/test_qc_window_order.py](../../../tests/test_qc_window_order.py), including a handler
that fires ahead of the fill; `listed_tickers`, the earnings rule in `add_custom`, is tested in
[tests/test_qc_earnings.py](../../../tests/test_qc_earnings.py). The native
[warm-up check](../../../deployments/quantconnect/warm_up_check.py) counts each window's and
indicator's values beside their distinct end times and the evaluations at which either holds
a value ending after `ctx.time`, and records the callback that performed each fill. The native
[coverage check](../../../deployments/quantconnect/coverage_check.py) declares the earnings,
ETF and monthly fundamental universe, wall-clock schedule, quote and SMA, RSI and Donchian
paths together and counts each declaration's arrivals.

## Scope

US equities (ETFs included) and US indices; market, market-on-open and market-on-close
orders, day or good-till-cancelled; native `SimpleMovingAverage`, `ExponentialMovingAverage`, `RelativeStrengthIndex` and
`DonchianChannel` indicators. Other
asset classes and indicator kinds raise `CapabilityError`.

The QuantConnect adapter's LEAN calls are verified by the backtests listed in
[validation](../../../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.
Deployment commands: [deployment](../../../docs/technical/DEPLOYMENT.md).
