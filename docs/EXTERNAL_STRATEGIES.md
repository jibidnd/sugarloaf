# External strategy packages

Keep infrastructure and proprietary research in separate repositories. The strategy
package depends on Sugarloaf; Sugarloaf never imports the private package. Local
editable installs make development convenient; production or reproducible runs
should pin a library revision/wheel and retain hashes of the actual source bundle.

## Graph and deployment

Nodes use the ordinary `inputs`, `outputs`, `requirements`, `triggers`, `evaluate`
interface. Compose them with `Graph`; finish with one PositionView output. For a
naive baseline, connect compatible forecasts to `MeanViewAggregator`, then
`PositiveSignalPositions`. Combine independently budgeted position branches through
`PositionNetter`; validate the final target with `PortfolioConstraintNode`.

Your strategy module exports a factory `deployment(parameter) -> Deployment`:

```python
from sugarloaf.deployment import Deployment, Environment
from sugarloaf.execution import ExecutionPolicy

# build_graph, config and ticker_bindings belong to your strategy package.
def deployment(parameter):
    return Deployment(
        name="my-strategy",
        strategy=config,
        graph_factory=lambda: build_graph(config),
        tickers=ticker_bindings,
        environment=Environment(),
        execution=ExecutionPolicy(),
    )
```

Use a fresh graph per run. The factory is ordinary Python, not a new configuration
language for node internals. Dates, capital and execution policy must be explicit
when reproducing another implementation. Framework defaults are not replication.
The factory decides which names it reads through `parameter`; passing an override
does not automatically change node or environment fields.

A factory that needs a file built on the platform declares a keyword `stored`:
`deployment(parameter, stored)`. On QuantConnect, `stored(key)` returns the text the
account keeps under that Object Store key, or `None` if there is none; it cannot
write. Use it for inputs prepared in a research run that may not leave the
platform, such as a table derived from licensed data. A factory may also declare
`fetch`: `fetch(url, headers)` is an HTTP GET through the platform that returns the
response text. QuantConnect limits it (about 100 calls a backtest, about 20 a minute
live), so read recorded responses through `stored` first and fetch only what is
missing: the same code then serves a backtest, which finds everything recorded, and
a live session, which fetches. A factory without these keywords is called with
`parameter` only.

An optional `report(context)` callback inspects `context.runtime.graph` and returns
summary diagnostics. Use `context.record(label, value)` for individual diagnostic
records; the adapter owns their serialization and output. The shared `ReportContext`
contains no native algorithm or provider client.

## Source manifest

The manifest is the TOML configuration file; these are two names for the same
input. TOML is its format, and manifest describes its role in selecting a run.
The QuantConnect adapter currently reads these selection fields. Keep reusable
manifests beside their node or strategy owner and limit them to source selection,
factory and parameters; choose the adapter in the command. The shared CLI selects
`--adapter quantconnect` and forwards the manifest path; other adapters can own
their manifest formats and run logic.

Example `strategies/strategy.toml` inside an external checkout:

```toml
name = "My strategy"
root = ".."
factory = "my_package.strategy:deployment"
include = ["my_package/strategy.py", "my_package/nodes/signal.py"]

[parameters]
ticker = "SPY"
```

### Manifest fields

Keep all deployment fields before `[parameters]`: TOML assigns subsequent keys
to that table until another table starts. Paths use the manifest's directory and
declared source root, rather than the command's current directory.

| Field | Required / default | Accepted meaning |
| --- | --- | --- |
| `name` | Required | String used when creating a QC project and as the default backtest name. `--name` overrides the backtest name; this does not set the factory's `Deployment.name` or the artifact folder. |
| `root` | Optional; `"."` | Source-root path resolved relative to the TOML directory; an absolute path also works. `include` and `entrypoint` resolve under it. Evidence goes to `<root>/artifacts/<TOML-stem>/`. |
| `factory` | Required for a Sugarloaf graph | String `importable.module:function`. The function accepts a parameter lookup callable and returns `Deployment`. Its module's `.py` file must be selected in `include`. Use exactly one of `factory` or `entrypoint`. |
| `entrypoint` | Required for a native QC algorithm | Path to the native Python algorithm under `root`, copied to the uploaded `main.py`. No Sugarloaf runtime or generated factory bridge is automatically included. Use exactly one of `entrypoint` or `factory`. |
| `include` | Optional; `[]`, but factory mode needs its dependencies | Array of root-relative files/directories. Directories recursively select `.py` files; individual files accept `.py` or `.ipynb`. Include the factory and all private imports. Native mode needs entries only for additional dependencies. |
| `project_id` | Optional | Integer ID of an existing QC project. A cloud run needs this or a project selected in saved `cloud.json`; `create` is an explicit setup operation when neither exists. This is not a credential. |
| `[parameters]` | Optional; empty mapping | Parameter-name/value table consumed by the selected factory or native algorithm. Prefer quoted strings, including numbers such as `lookback = "20"`. There is no universal strategy-parameter schema. |

The reader converts each parameter value with Python `str(value)`; for example,
integer `20` becomes `"20"`, while TOML `true` becomes `"True"`, not `"1"`.
Arrays/nested values are also stringified, not decoded into strategy objects.
Use the flat string table and the selected implementation's documented keys.
`--parameter NAME VALUE` overrides defaults for that upload/run; the factory must
actually read that name for it to affect the graph or environment.

Bundle validation checks the factory-reference syntax, source existence, root
containment, supported included-file suffixes, factory inclusion and infrastructure
filename collisions. It preserves real ancestor package initializers. Selected
sources cannot overwrite `sugarloaf/`, `main.py` or `deployment_config.py`.
The reader is not a strict schema validator: unknown top-level fields are currently
ignored, and strategy-parameter validation belongs to the selected Python code.
Use only the fields above; a misspelled field does not become a new setting.

Use distinct TOML filenames within a source root: the filename stem determines
the artifact directory. There is no separate artifact-output-path field. Credentials
belong in the authenticated LEAN environment, never in this file.

### What config.py means

`sugarloaf/deployment/config.py` defines the shared `Deployment` and `Environment`
classes. The example's `deployments/quantconnect/config.py` defines the Python
`ema_definition(parameter)` factory. The manifest selects that function by its
`factory` reference; the bundler generates `deployment_config.py` containing an
import alias, and QC calls that alias during algorithm initialization.

A private factory can live in `strategy.py`, `deployment.py` or another module;
the filename `config.py` has no special role. The Python factory constructs typed
strategy settings, environment/execution settings, and a fresh graph factory.
TOML supplies only the selection and explicitly consumed parameters. A separate
experiment `config.toml` can have a different study-owned schema.

Include every private import the selected strategy needs. Real ancestor package
initializers are included too, so keep them free of unrelated strategy imports.
Directories select all their Python files; notebooks require explicit file entries.
The bundle rejects paths outside `root` and replacements of framework modules.
An optional `project_id` reuses an existing QC project; otherwise `create` selects
a new project once and saves its selection in local adapter state. Keep this optional
provider binding out of portable Lab manifests. Distinct concurrent studies should
use distinct projects.

From the external checkout, with Sugarloaf available to the authenticated LEAN
Python environment:

```powershell
sugarloaf package --adapter quantconnect --manifest strategies/strategy.toml
# Review artifacts/strategy/bundle.json before the first upload.
# Only if neither the manifest nor saved state selects a project:
sugarloaf create --adapter quantconnect --manifest strategies/strategy.toml
sugarloaf run --adapter quantconnect --manifest strategies/strategy.toml --parameter ticker SPY
```

`run` requires a selected project, uploads sources and parameters, compiles, waits
for a completed backtest and retrieves results/logs. Every action requires a
manifest. Python callers use `from sugarloaf.adapters.quantconnect.cloud.backtests import run_backtest`
and pass the manifest path plus optional `parameters` and `name`. See the
[deployment reference](technical/DEPLOYMENT.md) for individual actions and polling
limits; LEAN owns network/authentication/retries, and timeout does not cancel a job.

Skip `create` if a project is already selected. Each manifest writes separate,
ignored `artifacts/<manifest-name>/` state. Upload applies manifest parameters and
invalidates any older compile/run selection. Run snapshots preserve source hashes,
parameters, available Git revisions, compile and backtest IDs. Full source bundles
and results are private when the strategy is private; keep them out of public Git.

Native reference algorithms use `entrypoint = "path/to/reference.py"` instead of
`factory`; they upload that source as `main.py`. No framework code is needed unless
explicitly requested through selected sources.
