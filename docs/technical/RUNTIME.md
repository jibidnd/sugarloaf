# Runtime: the graph runner

Source: [runner](../../sugarloaf/runtime/runner.py). Tests: [runner tests](../../tests/test_runner.py).

`GraphRunner(graph, adapter, *, constraints=None, policy=None, identity_limit=100_000)` runs
one compiled graph over one adapter. It reads every node's declarations, sets them up on the
adapter, dispatches the adapter's callbacks to the nodes in topological order, carries views
along the graph edges and, in a trading run, hands a changed terminal position target to the
adapter for execution. It has no clock of its own: every callback carries the platform's
time (`adapter.time`, or `Arrival.time` for a slice).

| Argument | Meaning |
| --- | --- |
| `graph` | A `CompiledGraph` with fresh node instances |
| `adapter` | An object implementing the [adapter interface](ADAPTERS.md#the-adapter-interface) |
| `constraints` | `PortfolioConstraints` the terminal target must satisfy; default long-only, unlevered |
| `policy` | `ExecutionPolicy` passed to `adapter.execute`; see [execution](EXECUTION.md) |
| `identity_limit` | How many recent view identities the runner remembers for the one-identity-one-content check |

`runner.trading` is `graph.emits_positions()`. A trading run executes targets; any other
graph is a forecast run and archives views.

## Construction and start

The constructor gathers declarations (`_gather`) and checks them: universe names declared
twice with different definitions, two feed declarations of one node with the same `name`,
repeated indicator names in a node, schedule names repeated anywhere in the graph, and a
selection naming a universe no node declares all raise `ContractError`.

`start()` is called once, before the platform's clock starts. On QuantConnect the launcher
calls it; on the memory adapter `MemoryAdapter.attach(runner)` does. It calls the adapter's
setup methods in this order:

1. `adapter.subscribe(declaration, instruments)` once per feed key, for the union of the
   explicit instruments of every node declaring that key, with the declaration of the
   largest `window`. Feeds are set up finest resolution first, because a platform gives a
   security one subscription and a coarser declaration on it is consolidated from that.
2. `adapter.add_universe(definition, feeds)` per universe, with the feeds (largest window
   per key) of the declarations that select it.
3. `adapter.add_custom(declaration)` per distinct `Custom` declaration.
4. `adapter.add_indicator(declaration, instruments)` per indicator, with its explicit
   instruments; universe members get theirs from the adapter when they join.
5. `adapter.schedule(declaration)` per schedule.
6. `adapter.warm_up(declaration)` once, with the longest `WarmUp` in the graph: the
   coarsest resolution with the largest count when every request is a count, the longest
   span when every request is a span, and otherwise a span covering both.

## Callbacks

| Method | Called by the adapter when | What runs |
| --- | --- | --- |
| `on_data(arrival)` | The platform delivers a slice | Every node whose declared part of the slice is not empty, then every node with a producer whose outputs changed, in topological order; then the terminal check |
| `on_securities_changed(universe, added, removed, warming_up=False)` | A universe's membership changed | `on_securities_changed` of every node that declares or selects that universe |
| `on_scheduled(name, warming_up=False)` | A declared schedule fired | `on_scheduled` of the owning node, then `update` (with an empty slice) of every node downstream whose producer changed; then the terminal check |
| `on_order_event(update)` | The platform reports order feedback | Appends the `OrderUpdate` to `runner.updates` (last 10,000) |
| `finish()` | The run ends | Returns a summary for the run's `RUN_SUMMARY` line |

`on_data` and `on_scheduled` each increment `runner.steps`, the value nodes read as
`ctx.sequence`.

### The slice a node receives

`_slice_for(node_id, arrival)` builds a node's `Slice` from the adapter's `Arrival`: for
each `Bars` or `Quotes` declaration, the delivered bars of its key for the instruments its
selection resolves to now (universe selections resolve through `adapter.members`); for each
`Custom` declaration, the records of its kind filtered to its selection; and the lifecycle
records (dividends, splits, delistings, symbol changes) of every instrument the node
selects. Named declarations go to `data.series(name)`.

### Running one node

`_run` builds the node's context, collects its upstream views, validates every port
against its `InputSpec`, calls the callback and validates the returned outputs against the
node's `OutputSpec`s and these rules (`_check`):

- each `view_id` appears once in an emission;
- `metadata.source` is the node id and `metadata.cutoff` is at most the callback time;
- a `view_id` seen earlier in the run carries the same contents.

A view whose `expires_at` is at or before the callback time is dropped from the output
before these checks: a node restating an old view after it expired withdraws it, as the
view would already have dropped out of a consumer's inputs.

`NotReady` and `NumericalError` from a callback or from port validation replace the node's
outputs with nothing and, outside warm-up, append a `Diagnostic(code, node, time, message)`
to `runner.diagnostics` (last 2,000; `runner.diagnostic_count` counts all). Any other
exception propagates to the adapter and from there to the platform.

The returned outputs replace the node's previous outputs entirely. The node's consumers run
in the same callback when the new outputs differ from the old.

### Views along edges

A consumer receives, per port, the current views of every edge into it, concatenated in edge
order, with views whose `expires_at` is at or before the callback time removed. Views stay
current until their producer replaces them; an upstream node that does not run keeps its
outputs.

### Warm-up

Every callback carries the platform's warm-up state (LEAN's `is_warming_up`): `on_data` in
`Arrival.warming_up`, `on_scheduled(name, warming_up)` and
`on_securities_changed(universe, added, removed, warming_up)` as an argument. Nodes read it
as `ctx.warming_up`. While it is true the nodes run as usual, so node state, windows and
indicators fill, but their outputs are discarded, nothing is archived, no diagnostic is
recorded and no target reaches `adapter.execute`.

## Terminal target

After `on_data` and `on_scheduled`, outside warm-up, a trading run takes the terminal
port's views. When there is exactly one `PositionView`, it differs from `runner.last_target`
and it has not expired, the runner checks it against `constraints`:

- weight targets are taken as weights; quantity targets are converted at the adapter's marks
  and NAV, and instruments without a mark are left out;
- the target is merged over current holdings (instruments the target omits stay held) and
  passed to `violations(effective, constraints, current)`, which checks gross, net, position
  bounds and turnover;
- a violation appends a `TARGET_REJECTED` diagnostic and sends nothing.

A target that passes becomes `runner.last_target` and goes to
`adapter.execute(target, policy)`; the returned `OrderRecord`s are appended to
`runner.orders` (last 10,000). A restated identical target sends nothing.

## Forecast runs

When the output port does not declare exactly `(PositionView,)`, every view any node emits
outside warm-up is recorded in `runner.archive`, a `ForecastArchive`, with the node id, port
and callback time. Position views are ignored by the archive. At the end of the run the
QuantConnect launcher scores the archive against prices and writes the metrics to the
backtest log; see [run output](#run-output) and [research](RESEARCH.md).

## Run output

A run's output goes to the backtest log and to the backtest's runtime statistics; the caller
reads both through QuantConnect's REST API and saves them on disk, by backtest id. The Object
Store holds persistent things only: fitted models and datasets reused across projects, which a
deployment factory reads through `stored` (`launcher.stored_reader`). `launcher.finish`, called
from `SugarloafAlgorithm.on_end_of_algorithm` at the platform's last clock, runs the strategy
report, computes the view metrics and writes:

| Log line | Content | Character budget |
| --- | --- | --- |
| `RUN_SUMMARY` | Deployment name, mode, environment, execution policy, `runner.finish()`, member counts per universe, order and update counts, node configs, the report's return value, its record labels, the error list | 10,000 (`SUMMARY_BUDGET`) |
| `VIEW_METRICS` | `view_metrics(archive, prices, clock)` from [`sugarloaf.research.view_metrics`](../../sugarloaf/research/view_metrics.py) | 32,000 (`METRICS_BUDGET`) |
| `VIEW_SAMPLE` | The first 20 archived views of each node, one per value, as `encode_archive` encodes a record | 16,000 (`SAMPLE_BUDGET`) |
| `RUN_RECORD` | Each `ReportContext.record(label, value)` | 12,000 (`RECORD_BUDGET`) |

Each value is JSON written as `<TAG> <part>/<parts> <text>` over as many consecutive lines as
it needs, at most 8,000 characters each: QuantConnect keeps 8,192 characters of a log line,
the timestamp LEAN adds included, and drops the rest without error. A value that would pass
its budget is reduced (`VIEW_METRICS` loses per-node groups, then per-node summaries) or left
out, and the error list says which. `algorithm.set_runtime_statistic` carries the headline:
views issued, views scored, and per view kind its primary metrics (score: hit rate, F1,
top-minus-bottom decile return; distribution: mean pinball loss, 90% coverage, mean log
score; ranking: mean Kendall tau). An exception from the strategy report is recorded, the
output is written, and the exception is raised again; LEAN then reports the error message
and keeps no log line of that final step.

QuantConnect caps the log output of a backtest by organization tier
([log quotas](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources#09-Log-Quotas),
read 15 November 2026): 10 KB per backtest and 3 MB per day on the Free tier, 100 KB and 3 MB
on Quant Researcher, 1 MB and 10 MB on Team, 5 MB and 50 MB on Trading Firm. Reading or
downloading logs counts toward the daily quota. The organization that runs the native checks
is on Quant Researcher (`organizations/read` reports a 102,400-byte limit per backtest), so the
four budgets sum to 70,000 characters, and a one-node forecast run such as `ema_forecasts`
writes about 22,000. Lines a node or report logs itself count toward the same quota and come
earlier in the log. On a tier with a larger quota, raise the budgets in
[launcher.py](../../sugarloaf/adapters/quantconnect/launcher.py).

`cloud.backtests.read_run_output(client, project_id, backtest_id)` reads a finished backtest's
log through `read_all_logs`, joins and decodes the lines (`parse_run_output`) and returns
`{"summary", "metrics", "samples", "records", "errors"}` with the project, backtest id and the
number of log lines read; `save_run_output(output, path)` writes it as JSON and refuses to
replace a file with different contents unless `overwrite=True`. `run_backtest` saves the output
of each run it completes as `artifacts/<manifest stem>/<backtest id>-output.json`, and
`sugarloaf output --adapter quantconnect --manifest M --backtest-id ID [--out PATH]` reads and
saves the output of an earlier run.

## Reads

| Member | Content |
| --- | --- |
| `runner.outputs(node_id, port=None)` | A node's current views, by port or for one port |
| `runner.terminal()` | The output port's current views |
| `runner.last_target` | The last executed `PositionView` |
| `runner.orders`, `runner.updates` | `OrderRecord`s returned by `execute`; `OrderUpdate`s from the platform |
| `runner.archive` | The `ForecastArchive` of a forecast run |
| `runner.diagnostics`, `runner.diagnostic_count` | Recent diagnostics and their total count |
| `runner.finish()` | `steps`, `diagnostic_count`, the last 20 diagnostics, order and forecast counts, the last target's id |
