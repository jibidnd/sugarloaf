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

## Scope

US equities (ETFs included) and US indices; market, market-on-open and market-on-close
orders, day or good-till-cancelled; native `sma`, `ema`, `rsi` and `dch` indicators. Other
asset classes and indicator kinds raise `CapabilityError`.

[Adapter tests](../../../tests/test_quantconnect_adapter.py) run the adapter against
[a stand-in for LEAN](../../../tests/fake_lean.py); they establish the calls made, not LEAN's
behaviour. Native evidence for the current adapter is pending; see
[validation](../../../docs/VALIDATION.md#redesign-of-14-november-2026).
Deployment commands: [deployment](../../../docs/technical/DEPLOYMENT.md).
