# Adapters

An adapter maps each Sugarloaf declaration and read to one platform call. It owns the
platform objects (subscriptions, consolidators, rolling windows, indicators, universes,
scheduled events, orders) and the binding between `InstrumentId` values and platform
symbols. It owns no graph logic: the runner decides which node runs, and the adapter
forwards the platform's callbacks to the runner.

Source: [interface](../../sugarloaf/adapters/protocol.py),
[QuantConnect adapter](../../sugarloaf/adapters/quantconnect/adapter.py),
[launcher](../../sugarloaf/adapters/quantconnect/launcher.py),
[algorithm entry point](../../sugarloaf/adapters/quantconnect/main.py),
[memory adapter](../../sugarloaf/adapters/memory/adapter.py) and
[memory indicators](../../sugarloaf/adapters/memory/indicators.py).

## The adapter interface

`Adapter` in `sugarloaf.adapters.protocol` is a protocol; an adapter implements these
methods and calls the runner's callbacks (`adapter.runner`).

| Kind | Method | Contract |
| --- | --- | --- |
| Setup | `subscribe(declaration, instruments)` | Subscribe a `Bars` or `Quotes` feed for explicit instruments, consolidate to its `interval`, keep its window, fill the window from history |
| Setup | `add_universe(definition, feeds)` | Create the membership rule; members get `feeds` when they join |
| Setup | `add_custom(declaration)` | Register a `Custom` dataset |
| Setup | `add_indicator(declaration, instruments)` | Create and warm the native indicator per instrument |
| Setup | `schedule(declaration)` | Register a scheduled callback that calls `runner.on_scheduled(name, warming_up)` |
| Setup | `warm_up(declaration)` | Ask the platform to replay history before the start |
| Read | `time` | The platform clock, aware UTC |
| Read | `window(declaration, instrument)` | Newest-first window of completed bars |
| Read | `indicator(declaration, instrument)` | An `IndicatorReading` |
| Read | `members(universe)` | Current members |
| Read | `history(request)` | Past bars ending at or before now, oldest first per instrument |
| Read | `calendar(instrument)` | A `Calendar`: `next_close`, `next_open`, `is_open`, `session` |
| Read | `portfolio()` | A `Portfolio` record |
| Action | `execute(target, policy)` | Turn a `PositionView` into platform orders; return `OrderRecord`s |
| Action | `save(key, text)` | Keep a result where the platform keeps files |

`Arrival` is what an adapter hands `runner.on_data`: the callback's `time`, `bars` and
`quotes` keyed by declaration key and then instrument, `custom` keyed by kind, the four
lifecycle tuples, and `warming_up`. `IndicatorReading(is_ready, value, time, values,
outputs)` is an indicator read now: `values` are `(time, value)` pairs newest first, at most
the declared window; `outputs` carries named bands.

## QuantConnect

`QuantConnectAdapter(algorithm, tickers, *, slippage=0.0005, leverage=1.0,
borrow_data=None, lean=None)` wraps a `QCAlgorithm`. `tickers` binds the instruments nodes
name explicitly to a ticker string (an equity) or a `Ticker(ticker, asset_class)` record
(`equity` or `index`); universe members are bound by LEAN's permanent security id
(`bind(symbol)` creates `InstrumentId(str(symbol.id))`). `lean` is the `AlgorithmImports`
namespace, injected by tests.

### Mapping

Each row is read off `adapter.py`; `algorithm` is the `QCAlgorithm`.

| Sugarloaf construct | LEAN call made by the adapter |
| --- | --- |
| Adapter construction | `algorithm.add_security_initializer(...)` setting `ConstantSlippageModel(slippage)` and `set_leverage(leverage)` per security, plus `set_shortable_provider(InteractiveBrokersShortableProvider())` and `ShortMarginInterestRateModel()` when `borrow_data="interactive_brokers"`; `settings.daily_precise_end_time = True`, `settings.seed_initial_prices = True` |
| `subscribe(Bars or Quotes)` | `Symbol.create(ticker, SecurityType.EQUITY or INDEX, Market.USA)`, then `algorithm.add_security(symbol, resolution, fill_forward=..., extended_market_hours=..., data_normalization_mode=...)` the first time an instrument is named (its subscription period is the finest declared; a coarser later declaration raises); one `RollingWindow[TradeBar](window)` (or `RollingWindow[QuoteBar]`) per instrument, filled from one `history[TradeBar](symbols, count, resolution)` call at the security's subscribed resolution, performed on first touch after `initialize` (the window's consolidator handler, `on_data`, a read, `on_securities_changed` or a scheduled function, with the clock at or after the start of a declared warm-up replay) and limited to bars ending before the bar in hand or the clock; a consolidated window is filled through its live consolidator while that holds no data, otherwise through a consolidator of its own. Every bar enters a window through `add_in_order`: it is added only when its end time is later than that of the window's newest bar, so a window holds each bar period once, newest first, and no bar ending after the clock ([adapter README](../../sugarloaf/adapters/quantconnect/README.md#windows-and-warm-up)) |
| `Bars.interval` / `Quotes.interval`, or a declaration coarser than the security's subscription | `TradeBarConsolidator(interval)` (or `QuoteBarConsolidator`), registered with `subscription_manager.add_consolidator(symbol, consolidator)`; its `data_consolidated` handler adds the bar to the window under the same rule and marks it for the next slice. A one-day interval uses a calendar function returning `CalendarInfo(open, close - open)` from `exchange.hours.get_next_market_open` and `get_next_market_close`, so the bar is the exchange session |
| `add_universe(LiquidityUniverse or FundamentalUniverse)` | `algorithm.add_universe(FundamentalUniverse.usa(selector, settings))` |
| `add_universe(ETFConstituentsUniverse)` | `algorithm.add_universe(algorithm.universe.etf(ticker, settings, selector))` |
| Universe settings | `UniverseSettings(algorithm.universe_settings)` with `asynchronous = False`, `minimum_time_in_universe`, the finest feed `resolution`, `fill_forward`, `extended_market_hours`, `data_normalization_mode`; `settings.schedule.on(date_rules.week_start() / month_start() / year_start())` for weekly, monthly and yearly cadence |
| Universe selector | Converts each row to a `SelectionRow` (`price`, `volume`, `dollar_volume`, `has_fundamental_data`, or `weight`) or a `FundamentalRecord`, applies `select(...)`, returns the selected symbols; `NotReady` returns `Universe.UNCHANGED` |
| `Custom("earnings")` | `algorithm.add_data(EODHDUpcomingEarnings, "earnings")`; per slice `data.get(EODHDUpcomingEarnings)` gives `report_date` and `report_time`. `add_custom` registers the security id of each equity the selection names (`Symbol.create`, through `listed_tickers`), so a listing maps to the declared instrument whether or not another declaration subscribes it; a named instrument without an equity ticker binding raises `CapabilityError` |
| `Custom("fundamentals")` | `algorithm.securities[symbol].fundamentals`, read once per day |
| `Custom("borrow")` | `security.shortable_provider.fee_rate(symbol, time)` and `shortable_quantity(symbol, time)`, read once per day; no record under `NullShortableProvider` |
| `add_indicator(Indicator)` | `SimpleMovingAverage(name, period)`, `ExponentialMovingAverage(name, period[, smoothing])`, `RelativeStrengthIndex(name, period, MovingAverageType)` or `DonchianChannel(name, period)`; a consolidator of the declaration's period (`IdentityDataConsolidator[TradeBar]()` at the security's own resolution, otherwise the `TradeBarConsolidator` used for windows) registered with `subscription_manager.add_consolidator(symbol, consolidator)`, whose `data_consolidated` handler feeds the indicator through `update_in_order` (`indicator.update(end_time, field)`, or the whole bar for Donchian, only when the bar ends after `indicator.current.end_time`); `indicator.window.size = window`; `algorithm.warm_up_indicator(symbol, indicator, resolution or interval[, selector])` on first touch (its consolidator handler, a read, `on_data`, `on_securities_changed` or a scheduled function), where `selector` is the `Field` member of the declared field when it is not the close |
| `schedule(Schedule)` | `algorithm.schedule.on(date_rule, time_rule, fn)` with `date_rules.every_day / week_start / month_start / month_end / year_start`, given the calendar symbol, or SPY's when the schedule names none (`_default_calendar`; SPY is added at daily resolution as a calendar-only security when no declaration subscribes it), and `time_rules.after_market_open(symbol, minutes)`, `before_market_close(symbol, minutes)` or `at(hour, minute, DateTimeZoneProviders.tzdb[timezone])`; `fn` calls `runner.on_scheduled(name, algorithm.is_warming_up)` |
| `warm_up(WarmUp)` | `algorithm.set_warm_up(count, resolution)` or `algorithm.set_warm_up(span)` |
| `time` | `algorithm.utc_time` |
| `window(declaration, instrument)` | The adapter's `RollingWindow`, read through `NativeWindow` (bars converted on access) |
| `indicator(declaration, instrument)` | `indicator.is_ready`, `indicator.current.value`, `current.end_time`, `indicator.window[i]`; Donchian adds `upper_band.current.value` and `lower_band.current.value` |
| `members(universe)` | Membership kept by the adapter from `on_securities_changed` |
| `history(HistoryRequest)` | `algorithm.history[TradeBar](symbols, count, resolution, fill_forward=False, extended_market_hours=..., data_normalization_mode=...)`, or with `(start, end)` for a span; a request with an `interval` raises `CapabilityError` |
| `calendar(instrument)` | `securities[symbol].exchange.hours`: `get_next_market_close`, `get_next_market_open`, `is_open`, `get_previous_market_open` (through `NativeCalendar`) |
| `portfolio()` | `securities[symbol].holdings.quantity` and `.price`, `algorithm.portfolio` holdings, `portfolio.total_portfolio_value`, `portfolio.cash`, `transactions.get_open_orders()` with `get_order_ticket(id).quantity_filled`, `algorithm.account_currency` |
| `execute(target, policy)` | Per instrument, sorted: `transactions.cancel_order(id, "superseded")` for open orders tagged with another target (when `policy.cancel_on_new_target`); lot size from `securities[symbol].symbol_properties.lot_size`; size with `desired_quantity` and `order_quantity`; send `market_order`, `market_on_open_order` or `market_on_close_order(symbol, quantity, asynchronous=True, tag=target_id, order_properties=OrderProperties())` with `time_in_force` `TimeInForce.DAY` or `GOOD_TIL_CANCELED`; an invalid ticket is recorded as `rejected` with `get_most_recent_order_response()` |
| Order feedback | `on_order_event(event)`: `transactions.get_order_ticket(event.order_id).quantity_filled`, `get_order_by_id(...).tag`, status mapped to an `OrderUpdate`, forwarded to `runner.on_order_event` |
| Slice | `on_data(slice)`: `slice.bars` or `slice.quote_bars` for instruments read at their subscription's period (each bar is also offered to its window through `add_in_order`; the arrival carries it either way), bars completed since the last slice for instruments with a consolidator; `slice.dividends`, `slice.splits` (split warnings skipped), `slice.delistings`, `slice.symbol_changed_events`; `warming_up = algorithm.is_warming_up`; forwarded to `runner.on_data` |
| Membership change | `on_securities_changed(changes)`: `changes.added_securities` and `removed_securities`; windows and consolidators created or removed (`subscription_manager.remove_consolidator`), indicators created, or their consolidators removed, new members' windows filled from `history[TradeBar]` through their consolidators; forwarded to `runner.on_securities_changed` |
| `save(key, text)` | `algorithm.object_store.save(key, text)` |
| Run start | `set_start_date`, `set_end_date`, `set_time_zone`, `set_account_currency`, `set_cash`, `set_brokerage_model(BrokerageName, AccountType)`, `set_benchmark` (in `launcher.start`); one `algorithm.debug("SUGARLOAF_RUN ...")` line |
| Run end | `on_end_of_algorithm` calls `launcher.finish`: `object_store.save` of `result.json` (and `forecasts.json` in a forecast run) under `sugarloaf/results/<project_id>/<algorithm_id>/`; one `algorithm.debug("SUGARLOAF_RESULT ...")` line with the key |

### Callbacks

`SugarloafAlgorithm` in [main.py](../../sugarloaf/adapters/quantconnect/main.py) forwards
LEAN's callbacks: `initialize` loads the deployment (`launcher.load`) and calls
`launcher.start`, which builds the adapter and the runner, sets `adapter.runner` and calls
`runner.start()`; `on_data`, `on_securities_changed` and `on_order_event` call the adapter
method of the same name; `on_end_of_algorithm` calls `launcher.finish`. Scheduled functions
and consolidator handlers are registered by the adapter itself.

`launcher.load(factory, algorithm)` calls the deployment factory with
`algorithm.get_parameter`, plus `stored` (read-only Object Store reads) and `fetch`
(`algorithm.download`) when the factory's signature asks for them.

### Time

LEAN timestamps bars in exchange-local time. The adapter converts each with the security's
exchange time zone (`securities[symbol].exchange.time_zone`), looked up once per security.
`daily_precise_end_time` stamps a daily equity bar at 16:00 New York. `ctx.time` is
`algorithm.utc_time`. There is one clock, the platform's; a backtest and a live run use it
alike.

### Universe members

When LEAN reports securities added or removed, the adapter compares them with the last
selection of each universe, creates windows, consolidators and indicators for members that
joined, removes them for members that left, fills the joined members' `Bars` windows with
`history[TradeBar]` when the feed keeps more than one bar or has an interval
(`window × interval / resolution` feed bars, passed through the consolidator when the feed
has an interval), records the membership and calls
`runner.on_securities_changed(name, joined, left, warming_up)`. Consolidated history bars are not
delivered as news in the next slice.

### Corporate actions

LEAN adjusts holdings and, for `adjusted` normalization, prices. The adapter forwards
dividends, splits, delistings and symbol changes as records in the slice, and changes no
window, indicator or view because of them. A node that needs to react to them reads
`data.splits` and the other lifecycle tuples.

### Scope

Data: US equities (ETFs included) and US indices. Orders: market, market-on-open and
market-on-close, day or good-till-cancelled. Indicators: `sma`, `ema`, `rsi`, `donchian`.
The adapter refuses other asset classes and indicator kinds with `CapabilityError`. The
[validation record](../VALIDATION.md#redesign-of-14-november-2026) lists which rows of the
mapping table have native evidence.

The QuantConnect adapter has local tests only for its bar-order rules
(`tests/test_qc_window_order.py`); its LEAN calls are verified by the backtests listed in
[validation](../VALIDATION.md#native-checks), run with `tools/native_checks.py`.

## Memory

`MemoryAdapter(start, *, account=100_000.0, calendars=None, histories=None, lot_sizes=None,
fills=True)` implements the same interface for tests and examples: it replays the bars,
membership changes and scheduled times the caller hands it, keeps windows and indicators from
them, and fills orders at the price last given. The caller drives it:

| Method | Effect |
| --- | --- |
| `attach(runner)` | Binds the runner and calls `runner.start()` |
| `deliver(time, bars=(), quotes=(), custom=None, dividends=(), splits=(), delistings=(), symbol_changes=(), warming_up=False)` | Advances the clock (never backwards), routes each bar to every subscribed feed whose resolution period equals the bar's duration, consolidates, updates windows and indicators, builds an `Arrival` and calls `runner.on_data` |
| `fire(name, time=None, warming_up=False)` | Calls `runner.on_scheduled(name, warming_up)` for a declared schedule |
| `set_members(universe, members, warming_up=False)` | Replaces membership, creates windows and indicators for new members, calls `runner.on_securities_changed` |
| `refresh(universe, rows, at=None)` | Applies the universe rule (`select`) to `SelectionRow`s, then `set_members` |
| `set_price(instrument, price)` | Sets a mark (bars set marks too) |
| `fill(order_id)` | Fills an open order at the current mark, for adapters built with `fills=False` |

Inputs the caller supplies: `calendars` maps instruments to
`Sessions(regular, extended=())`, tuples of `(open, close)`; `histories` maps instruments to bars served by `history`; `lot_sizes`
defaults to one. With `fills=True`, `execute` fills each order immediately at the current
mark and reports it through `runner.on_order_event`; `cancel_on_new_target` cancels every
open order in the target's instruments first, whatever its tag. Sizing uses the account read
before those cancellations, so within that call a cancelled order still counts as pending. `saved` holds what `save` was given.

Indicators come from [memory/indicators.py](../../sugarloaf/adapters/memory/indicators.py):
SMA, an EMA seeded by its first value, Wilder's RSI and a Donchian channel. An indicator
updates on completed bars of a subscribed feed with the same key as `Indicator.bars`, so a
node declares the matching `Bars` input. These formulas serve tests of node logic; they are
not a parity claim with LEAN.

The memory adapter has no slippage, fees, session calendar unless one is supplied, or data
it was not handed. [Memory adapter tests](../../tests/test_memory_adapter.py) and
[runner tests](../../tests/test_runner.py) use it.

## Writing another adapter

Implement the methods in the interface table over the platform's API, keep the platform's
objects keyed by declaration key and instrument, convert platform records to the records in
`sugarloaf.core.data` field by field, and forward each platform callback to the runner. Size
orders with `desired_quantity` and `order_quantity` from `sugarloaf.execution` so that every
adapter applies the same thresholds.
