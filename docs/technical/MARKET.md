# Market data and Context

Nodes declare what they may read; providers implement those reads. Requirements
describe data and service needs. Triggers separately decide when to evaluate.
The neutral contracts live in [market/contracts.py](../../sugarloaf/market/contracts.py);
service protocols and Context live in [core/services.py](../../sugarloaf/core/services.py).

## Requirements

| Requirement | Meaning |
| --- | --- |
| `BarsRequirement` | Selection, interval, lookback, session, normalization, fill-forward and maximum age |
| `QuotesRequirement` | Bid/ask observations, interval, lookback, session and freshness |
| `SeriesRequirement` | Named scalar history with availability convention and freshness |
| `IndicatorRequirement` | Native indicator over a bar source, immutable parameters and valid output window |
| `UniverseRequirement` | Named liquidity or historical ETF-constituent definition |
| `InstrumentRequirement` | Metadata/calendar access for a selection |
| `PortfolioRequirement` | Account capabilities and selection |

`Selection` contains explicit stable IDs or one universe name. Unknown membership
is different from a ready, explicitly empty universe. A requirement's existence
does not promise that every adapter supports it.

For an EMA with two retained outputs:

```python
from datetime import timedelta

from sugarloaf.core.types import InstrumentId
from sugarloaf.market.contracts import BarsRequirement, IndicatorRequirement, Selection

asset = InstrumentId("USA:SPY")
bars = BarsRequirement(
    Selection((asset,)), timedelta(minutes=5), normalization="raw"
)
ema = IndicatorRequirement(bars, "ema", (("period", 20),), window=2)
```

Declare `ema` in the node's requirements. Within `evaluate`, read
`ctx.market.indicator(ema, asset).points[-1].values["value"]`.
Period 20 controls the native indicator; window 2 controls retained ready outputs.
Indicator sources require `lookback=1`; native prefill establishes indicator state.
[Adapter resources](ADAPTERS.md#resources-prefill-and-ownership) define sharing,
prefill and reset behavior.

## Observation and availability

Bars have separate start, end and availability timestamps. Quotes and scalar
observations have observation/availability timestamps. All timestamps are aware;
adapters normalize to UTC. Reads admit only values with observation end/time at or
before `ctx.now` and availability at or before `ctx.available_at`.

A bar ending 10:01 and received 10:03 is unavailable to a 10:02 receipt boundary.
It can be read at 10:03 if admitted and fresh. Future history fetched for replay
does not become readable early. Native indicators additionally require admitted
source input; older data cannot rewind their state.

Readiness checks the filtered history count and maximum age. Excluding fill-forward
bars may leave too little history. Stale or missing data raise `NotReady`;
unsupported normalization or undeclared reads raise `CapabilityError`.
Normalization and return conventions are explicit assumptions, not cosmetic options.

## Context services

`Context` binds lazy reads to one provider version and batch:

| Attribute | Use |
| --- | --- |
| `market` | Bars, quotes, scalar series, indicators, membership and exact anchors |
| `instruments` | Metadata, tradability and session calendars |
| `portfolio` | Immutable authoritative account snapshot |
| `valuation` | Unit value in account currency |
| `costs` | Expected trading cost |
| `constraints` | Available portfolio limits |
| `now`, `available_at`, `snapshot_id` | Graph clock, receipt boundary and batch sequence |

A market-only provider need not supply portfolio, valuation or execution services.
Missing services fail explicitly. Account snapshots contain holdings, NAV, cash,
prices and open orders; they are read lazily rather than copied at Context creation.

`InstrumentRequirement(Selection())` permits metadata for input/account-dependent
instruments without subscribing to their prices. Empty portfolio selection means
the whole account. Restricted selection filters holdings/prices/orders but preserves
account-wide NAV/cash. Fields declare supported account capabilities rather than
individually guarded snapshot attributes.

`ctx.compute(key, inputs, parameters, version, function)` shares a hashable
calculation within the provider snapshot. It is not persistent model storage.
Provider mutation or Context closure invalidates reads with `SnapshotError`.
The runtime closes evaluation Contexts after processing; do not retain them.

## Units and exact anchors

Returns are decimal simple returns; horizons use aware intervals and ACT/365
calendar years. Quantities are instrument units. Weights are account-NAV fractions.
Unit valuation is mark × multiplier × account-currency FX; unsupported conversion
fails rather than guessing.

`ctx.market.anchor(instrument, at, convention)` requires the exact observation
from declared convention-compatible bar history. It neither substitutes a nearby
close nor fetches new data. Outcome resolution is separate and may use its own
explicit exact-anchor source. See [research outcomes](RESEARCH.md#outcomes-and-extensions).

## Ownership and changing declarations

`NodeOutputs.resources=None` retains the node's declarations. A
`ResourceUpdate(requirements, triggers)` replaces them at the next batch boundary.
The runtime installs replacements before provider admission. Resource replacement
also activates the owner; it cannot alter services halfway through an evaluation.

Compatible subscriptions are shared among owners. Lookback/freshness do not create
separate backing subscriptions, although each read enforces its own requirement.
Requirements, readiness, membership and retained resources are distinct states.
Resources needed by holdings/open orders may remain after graph ownership changes;
cleanup never liquidates broker positions.

See [ProviderServices lifecycle](ADAPTERS.md#components-and-lifecycle) for
`install`, `apply`, `context`, `ready` and `schedule_due`.
[Memory tests](../../tests/test_adapter_memory.py) and
[QC boundary tests](../../tests/test_adapter_quantconnect.py) cover guarded reads,
resource changes, readiness and lazy snapshots. Native acceptance limits are
listed in [validation](../VALIDATION.md).
