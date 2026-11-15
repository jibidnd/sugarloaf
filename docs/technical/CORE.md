# Core: declarations, data, nodes, views and graphs

Source: [declarations](../../sugarloaf/core/inputs.py), [records and slices](../../sugarloaf/core/data.py),
[node interface](../../sugarloaf/core/node.py), [graph compiler](../../sugarloaf/core/graph.py),
[universe selection](../../sugarloaf/core/universes.py), [views](../../sugarloaf/core/views.py),
[primitives](../../sugarloaf/core/primitives.py) and [errors](../../sugarloaf/core/errors.py).
The [runtime reference](RUNTIME.md) describes how `GraphRunner` uses these objects; the
[adapter reference](ADAPTERS.md) describes how a platform serves them. Nothing in
`sugarloaf.core` imports a platform.

## Units and identities

Every timestamp is timezone-aware; adapters convert platform times to UTC.
`InstrumentId(value)` is a stable identity independent of the ticker. On QuantConnect an
instrument named by a node is bound to a ticker by the deployment; a universe member is
bound by LEAN's permanent security id. `Interval(start, end)` requires `end > start`.
Returns are decimal simple returns. Position weights are fractions of account NAV.

## Declarations

A node lists what it needs as attributes. The runner reads them once, before the platform's
clock starts, and asks the adapter to set each one up (`GraphRunner.start`). A declaration
fetches nothing; it is a request the adapter translates into platform calls. All
declarations are frozen dataclasses in `sugarloaf.core.inputs`.

| Declaration | Fields (defaults) | Meaning |
| --- | --- | --- |
| `Selection` | `instruments=()`, `universe=None` | Explicit instruments or the name of one declared universe, never both |
| `Bars` | `selection`, `resolution="daily"`, `interval=None`, `window=1`, `normalization="adjusted"`, `extended_hours=False`, `fill_forward=False`, `name=None` | Trade bars at `resolution` (`minute`, `hour`, `daily`), consolidated to `interval` when set; the platform keeps the newest `window` completed bars for `ctx.window` |
| `Quotes` | `selection`, `resolution="minute"`, `interval=None`, `window=1`, `extended_hours=False`, `name=None` | Bid/ask bars, as traded |
| `Custom` | `kind`, `selection=Selection()` | A platform dataset by neutral name: `earnings`, `fundamentals` or `borrow` |
| `Universe` | `name`, `cadence="daily"` (keyword), `minimum_residence=timedelta(0)` (keyword) | Base class of the membership rules below; declare a subclass |
| `LiquidityUniverse` | `name`, `count`, `min_price=0`, `min_volume=0`, `require_fundamentals=False`, `cadence`, `minimum_residence` | The `count` securities with the largest dollar volume passing the filters |
| `ETFConstituentsUniverse` | `name`, `etf`, `count=None`, `min_weight=None`, `missing_weight="exclude"`, `cadence`, `minimum_residence` | Constituents of an ETF ranked by weight; `missing_weight` is `exclude`, `include` or `error` |
| `FundamentalUniverse` | `name`, `rule`, `version="1"`, `cadence`, `minimum_residence` | Members chosen by `rule(at, records)` over `FundamentalRecord` values; compared by name, version and settings, not by the rule object |
| `Indicator` | `name`, `kind`, `period`, `selection`, `resolution="daily"`, `interval=None`, `window=1`, `field="close"`, `normalization="adjusted"`, `parameters=()` | A platform-native indicator per selected instrument: `kind` is `sma`, `ema`, `rsi` or `donchian`; `parameters` holds extra settings such as `("smoothing_factor", 0.1)` or `("moving_average_type", "wilders")` |
| `Schedule` | `name`, `calendar=None`, `days="every_day"`, `after_open=None`, `before_close=None`, `at=None`, `timezone="America/New_York"` | A named callback time; exactly one of `after_open` (minutes), `before_close` (minutes) or `at` (naive wall time in `timezone`); `days` is `every_day`, `week_start`, `month_start`, `month_end` or `year_start` |
| `WarmUp` | `count=None`, `resolution="daily"`, `span=None` | History the platform replays through the graph before the first live slice, by bar count or by span |
| `HistoryRequest` | `instruments`, `count=None`, `span=None`, `resolution="daily"`, `interval=None`, `normalization="adjusted"`, `extended_hours=False` | A one-off read through `ctx.history`, by count or by span |

`Bars`, `Quotes`, `Custom` and the universe classes are `Input` subclasses and go in a
node's `inputs`; `Indicator` values go in `indicators`; `Schedule` values in `schedules`;
one `WarmUp` in `warm_up`. Constructors validate their values and raise `ContractError`:
an `interval` must be a whole multiple of the resolution period, above it and at most one
day; a `Schedule` anchored to the session needs a `calendar` instrument; `WarmUp` and
`HistoryRequest` take a count or a span, not both.

`Bars.key` and `Quotes.key` identify the feed and consolidation without the window and
name: two declarations with the same key share one subscription, one consolidator and one
window per instrument, sized to the largest `window` asked for. On QuantConnect a one-day
`interval` built from an intraday feed is the exchange session; the memory adapter groups
such bars by the date of their start timestamp. `Indicator.bars` is the bar feed the
indicator consumes. `Universe.period(at)` gives the cadence period containing a time.
`normalization` is `raw`, `adjusted` (splits and dividends), `split_adjusted` or
`total_return`.

`select(definition, rows, at)` in `sugarloaf.core.universes` applies a universe rule to
`SelectionRow` values (or `FundamentalRecord` values for a `FundamentalUniverse`). Ties
break on the instrument identity, a security listed twice counts once, and missing inputs
raise `NotReady`, which the QuantConnect adapter answers by leaving membership unchanged.

## Records and the slice

Adapters translate platform objects field by field into immutable records in
`sugarloaf.core.data`. Each has an aware UTC time.

| Record | Fields |
| --- | --- |
| `Bar` | `instrument`, `start`, `end`, `open`, `high`, `low`, `close`, `volume`, `fill_forward=False` |
| `QuoteBar` | `instrument`, `start`, `end`, `bid`, `ask`, `bid_size=None`, `ask_size=None`; property `mid` |
| `Dividend` | `instrument`, `time`, `amount`, `reference_price=None` |
| `Split` | `instrument`, `time`, `factor` (0.1 for 10-for-1), `reference_price=None` |
| `Delisting` | `instrument`, `time` |
| `SymbolChange` | `instrument`, `time`, `old_ticker`, `new_ticker` |
| `EarningsListing` | `instrument`, `time`, `report_date`, `timing` (`before_open`, `after_close`, `unknown`) |
| `FundamentalRecord` | `instrument`, `time`, `cik`, `market_cap`, `price`, `dollar_volume`, `sector` (Morningstar code) |
| `BorrowTerms` | `instrument`, `time`, `fee_rate`, `shortable` |
| `Observation` | `time`, `value`, `available_at` (defaults to `time`); used by outcome anchors |

`Slice` is what one node receives in `update`: the part of one platform callback that the
node declared, for the instruments its selections resolve to at that moment.

| `Slice` member | Content |
| --- | --- |
| `time` | The platform clock at the callback |
| `bars`, `quotes` | `{instrument: Bar}` and `{instrument: QuoteBar}` of unnamed `Bars` and `Quotes` declarations |
| `series(name)` | Bars of a named declaration; empty when none completed in this slice |
| `custom[kind]` | Records of a `Custom` declaration, filtered to its selection |
| `dividends`, `splits`, `delistings`, `symbol_changes` | Lifecycle records for the node's instruments |
| `empty`, `instruments()` | Whether the slice carries any record; every instrument it names |

A bar appears in a slice once, when it completes. `Window` (memory adapter) and
`NativeWindow` (QuantConnect adapter) are newest-first sequences of completed bars:
`window[0]` is the latest; `column(name, count)` and `oldest_first(count)` return the
newest `count` values oldest first.

## Node interface

A node is any object with the attributes and methods below; inheriting from the `Node`
protocol is optional. Omitted attributes take the defaults in `NODE_DEFAULTS`, so a node
declares only what it uses.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `inputs` | `Bars`, `Quotes`, `Custom`, universe declarations | Data the node receives in `update` |
| `indicators` | `Indicator` declarations | Native indicators read through `ctx.indicator(name, instrument)` |
| `schedules` | `Schedule` declarations | Named times delivered to `on_scheduled`; names are unique across the graph |
| `warm_up` | `WarmUp` or None | History replayed before the start; the runner takes the longest request in the graph |
| `ports` | `{name: InputSpec}` | Upstream view ports |
| `outputs` | `{name: OutputSpec}` | View ports the node emits |
| `config` | mapping | Parameters recorded in the run's result file |

| Callback | Called when |
| --- | --- |
| `update(ctx, data, views)` | A slice carries records the node declared, or an upstream node's outputs changed in this callback |
| `on_scheduled(ctx, name, views)` | A schedule the node declared fired |
| `on_securities_changed(ctx, added, removed)` | Membership of a universe the node declares or selects changed; `added` and `removed` are instrument tuples |

`update` and `on_scheduled` return the node's complete current output as
`{port: views}`. A port left out withdraws its previous views; `None` from `update` or
`on_scheduled` withdraws every port. A node that has nothing new (its instrument is absent
from the slice, for example) returns its previous output to keep it, as
[`EMAScore.update`](../../examples/ema.py) does. `on_securities_changed` returns nothing;
a node that keeps per-instrument state creates and drops it there.

A node owns its state between callbacks. The platform keeps rolling windows and indicator
values; the node reads them through its context.

### Context

The runner passes a `NodeContext` (`sugarloaf.runtime.runner`) as `ctx`. The `Context`
protocol in `sugarloaf.core.node` names what a node may use:

| Member | Returns |
| --- | --- |
| `ctx.node_id` | The node's id in the graph; use it as the view `source` |
| `ctx.time` | The platform clock, aware UTC |
| `ctx.warming_up` | True while the platform replays warm-up history |
| `ctx.sequence` | The runner's step count; with `node_id` it makes a run-unique view id |
| `ctx.window(declaration, instrument)` | The rolling window of a `Bars` or `Quotes` declaration, newest first |
| `ctx.indicator(name, instrument)` | An `IndicatorReading`: `is_ready`, `value`, `time`, `values` (newest first), `outputs` (Donchian `upper` and `lower`) |
| `ctx.members(universe)` | Current members of a declared universe |
| `ctx.history(request)` | `{instrument: bars oldest first}` for a `HistoryRequest` |
| `ctx.calendar(instrument)` | Session reads: `next_close(after, count)`, `next_open(after, count)`, `is_open(at)`, `session(at)` |
| `ctx.portfolio` | A read-only `Portfolio`: NAV, cash, holdings, marks, open orders |

Readiness is what the node checks itself: `reading.is_ready` for an indicator and
`len(window)` for a window. A node that cannot produce an output raises `NotReady`; the
runner withdraws the node's outputs and records a diagnostic.

## Views

Nodes exchange messages. A predictive claim is `View(target, statement, metadata)`;
a desired holding is a `PositionView`.

`Metadata(view_id, source, cutoff, expires_at, parents=(), model_version="1")` identifies
immutable content. `source` is the emitting node's id, `cutoff` the time of the
information used (at most `ctx.time`), `expires_at` the end of the view's authority and
later than `cutoff`. Parents are unique and exclude the view itself. One identity has one
content for the whole run: the runner refuses a reused `view_id` with different contents.

`Target(subject, quantity, time, units="decimal", convention="price_return",
components=())` states what a prediction concerns, including its horizon.
`return_target(instrument, interval)` builds a scalar return target and
`relative_return(left, right, interval)` the difference of two aligned returns.

| Representation | States | Answers |
| --- | --- | --- |
| `Estimate(value, forecast_error_scale, property="mean", level=None)` | One property of the target: mean, variance or the quantile at `level` | That property only |
| `Probability(events, probabilities, exhaustive=False)` | Probabilities of `ReturnEvent` intervals, each lower < return <= upper | The stated events only |
| `Distribution` (abstract) | A full law with `units`, `support` and `dimension` | What the subclass implements, plus what follows from it |
| `QuantileDistribution(levels, values, lower_bound, tail_scale_low, tail_scale_high)` | A scalar law from stated quantiles, linear between them, exponential tails | Moments in closed form, quantiles, CDF, density |
| `Ordering(groups, temperature, property="mean")` | Highest-to-lowest tied groups of the target's instruments | The order |
| `Score(value, calibration_id)` | A scalar on a named calibration | Nothing beyond itself |

Every representation has `supports(query)` and the queries `mean()`, `variance()`,
`quantile(p)` and `probability(lower, upper)`; a query the statement cannot answer raises
`CapabilityError`. `View` validates the pairing of target and statement. The named
subclasses fix one pairing: `ReturnView`, `RelativeReturnView`, `ProbabilityView`,
`DistributionView`, `RankingView` and `ScoreView`. A score is not a calibrated return or
probability.

`PositionView(positions, units, scope, metadata, attribution=None)` maps instruments to
desired amounts in `weight` (fraction of account NAV) or `quantity` units. An explicit
zero requests an exit; withdrawal, expiry and missing input request nothing.

## Ports and graphs

`InputSpec(accepts, min_count=1, max_count=None, statements=(), quantities=(),
properties=(), needs=())` declares the view classes a port accepts, its cardinality and
the queries (`needs`, such as `("mean",)`) every statement must answer.
`OutputSpec(emits, statements=(), quantities=(), properties=())` declares what a port
emits. `NodeInputs` is what a callback receives as `views`: `views["port"]` returns that
port's current upstream views, concatenated in edge order.

`Graph().add_node(id, node)`, `connect(source, port, destination, port)` and
`set_output(id, port)` build the wiring; `compile()` returns a `CompiledGraph` with nodes,
edges, `topological_order` and `output`. Compilation rejects unknown nodes or ports,
incompatible view classes or declared semantics, invalid cardinalities, disconnected
required ports, cycles, and an output node with outgoing edges. Independent nodes are
ordered alphabetically, so the order is deterministic.

`CompiledGraph.emits_positions()` is true when the output port declares exactly
`(PositionView,)`; the runner then runs the graph as a trading run. Any other output makes
a forecast run. Node instances are run state: build a new graph for each run, which is why
`Deployment.graph_factory` is a factory.

## Errors

| Error | Code | Runner behaviour |
| --- | --- | --- |
| `NotReady` | `DATA_NOT_READY` | Withdraws the node's outputs, records a `Diagnostic` |
| `NumericalError` | `NUMERICAL_FAILURE` | Same as `NotReady` |
| `ContractError` | `CONTRACT_INVALID` | Propagates: invalid wiring, units, identity or output |
| `CapabilityError` | `CAPABILITY_UNSUPPORTED` | Propagates: the adapter does not support the request |

## Writing a node

1. Declare the data (`inputs`, `indicators`, `schedules`, `warm_up`) and the ports.
2. In `update`, take the bar or record from `data`, read windows and indicators through
   `ctx`, and raise `NotReady` when a reading is not ready.
3. Build each view with an explicit target, horizon and units, and
   `Metadata(f"{ctx.node_id}:{ctx.sequence}", ctx.node_id, ctx.time, expiry)`.
4. Return the complete output `{port: views}`.

The [guide](../GUIDE.md#2-write-a-node) has a runnable example; the
[built-in nodes](../../sugarloaf/nodes/README.md) and [examples](../../examples/README.md)
are complete implementations. Tests: [runner](../../tests/test_runner.py),
[nodes](../../tests/test_nodes.py), [view axes](../../tests/test_view_axes.py).
