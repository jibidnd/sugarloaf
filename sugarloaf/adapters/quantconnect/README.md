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

`subscribe` fills a named instrument's `RollingWindow` from one `history` call ending at the
algorithm's start; a member's window is filled the same way when it joins. A `WarmUp`
declaration makes LEAN replay the period before the start through the same subscriptions and
consolidators, so the replay delivers bars the window already holds. Every bar reaches a
window through `add_in_order`: a bar is added only when its end time is later than the end
time of the window's newest bar (`window[0]`), so a window holds each bar period once,
newest first.

- A replayed bar ends at or before the newest bar from history and stays out of the window.
  The arrival still carries it in `data.bars`, with `ctx.warming_up` set, so nodes receive
  the replay as `WarmUp` describes.
- The first bar after warm-up ends after the last warm-up bar and is added.
- A late bar, one that arrives after a bar ending later than it, is handled the same way:
  it stays out of the window and the arrival carries it as LEAN delivered it. A node that
  must tell such a bar apart compares `data.bars[i].end` with `ctx.window(...)[0].end`.

Indicators follow the same rule. Each is built by class, fed by a consolidator of its period
(an identity consolidator at the security's own resolution) through `update_in_order`, and
warmed with `warm_up_indicator`. A bar updates the indicator only when it ends after
`indicator.current.end_time`. LEAN's indicators drop an input ending before the previous
one but accept a different bar object ending at the same time, which is what the warm-up
replay delivers for the last period `warm_up_indicator` read; the rule leaves it out.

`add_in_order` and `update_in_order` are module functions, tested without LEAN in
[tests/test_qc_window_order.py](../../../tests/test_qc_window_order.py). The native
[warm-up check](../../../deployments/quantconnect/warm_up_check.py) counts each window's and
indicator's values beside their distinct end times.

## Scope

US equities (ETFs included) and US indices; market, market-on-open and market-on-close
orders, day or good-till-cancelled; native `SimpleMovingAverage`, `ExponentialMovingAverage`, `RelativeStrengthIndex` and
`DonchianChannel` indicators. Other
asset classes and indicator kinds raise `CapabilityError`.

The QuantConnect adapter's LEAN calls are verified by the backtests listed in
[validation](../../../docs/VALIDATION.md#native-checks), run with `tools/native_checks.py`.
Deployment commands: [deployment](../../../docs/technical/DEPLOYMENT.md).
