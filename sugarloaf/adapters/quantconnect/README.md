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

**Fill.** `subscribe` creates a named instrument's `RollingWindow`; `add_indicator` builds
its indicators. The window's one `history` call and the indicator's `warm_up_indicator` run
at LEAN's first callback after `initialize` (`on_securities_changed`, `on_data` or a
scheduled function, whichever comes first), when LEAN has moved the clock to the start of
the warm-up replay: the start date minus the span of a declared `WarmUp`, or the start date
without one. The fill takes the history bars ending before the clock (`fill_before`); the
bar ending at the clock arrives in the slice being delivered. A member's window and
indicators are filled the same way when it joins, at that callback's clock. The replay then
delivers the bars from the fill's end up to the start date, in order.

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

`add_in_order`, `fill_before` and `update_in_order` are module functions, tested without
LEAN in [tests/test_qc_window_order.py](../../../tests/test_qc_window_order.py). The native
[warm-up check](../../../deployments/quantconnect/warm_up_check.py) counts each window's and
indicator's values beside their distinct end times, and the evaluations at which either
holds a value ending after `ctx.time`.

## Scope

US equities (ETFs included) and US indices; market, market-on-open and market-on-close
orders, day or good-till-cancelled; native `SimpleMovingAverage`, `ExponentialMovingAverage`, `RelativeStrengthIndex` and
`DonchianChannel` indicators. Other
asset classes and indicator kinds raise `CapabilityError`.

The QuantConnect adapter's LEAN calls are verified by the backtests listed in
[validation](../../../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.
Deployment commands: [deployment](../../../docs/technical/DEPLOYMENT.md).
