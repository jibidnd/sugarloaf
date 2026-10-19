# Market data and Context

Nodes declare what they may read; providers implement those reads. Requirements
describe data and service needs. Triggers separately decide when to evaluate.
The neutral contracts live in [market/contracts.py](../../sugarloaf/market/contracts.py);
service protocols and Context live in [core/services.py](../../sugarloaf/core/services.py).

## Requirements

| Requirement | Meaning |
| --- | --- |
| `BarsRequirement` | Selection, interval, lookback, session, normalization, fill-forward, maximum age and `require_all` |
| `QuotesRequirement` | Bid/ask observations, interval, lookback, session and freshness |
| `IndicatorRequirement` | Native indicator over a bar source, immutable parameters and valid output window |
| `UniverseRequirement` | Named liquidity, historical ETF-constituent or fundamental-rule definition |
| `FundamentalsRequirement` | The selected securities' latest fundamental records, with a maximum age |
| `ShortAvailabilityRequirement` | The selected securities' latest borrow terms |
| `InstrumentRequirement` | Metadata/calendar access for a selection |
| `PortfolioRequirement` | Account access and selection |

`Selection` contains explicit stable IDs or one universe name. Unknown membership
is different from a ready, explicitly empty universe. A requirement's existence
does not promise that every adapter supports it.

For an EMA with two retained outputs:

```python
from datetime import timedelta

from sugarloaf.core.primitives import InstrumentId
from sugarloaf.market.contracts import BarsRequirement, IndicatorRequirement, Selection

asset = InstrumentId("USA:SPY")
bars = BarsRequirement(
    Selection((asset,)), timedelta(minutes=5), normalization="raw"
)
ema = IndicatorRequirement(bars, "ema", (("period", 20),), window=2)
```

A requirement's interval is also how often the node can act. An adapter
subscribes the coarsest feed that serves it, so a node that declares only daily
bars is evaluated once a session, at the close. A node that must act at 15:50
declares minute data, or a session schedule
([adapter feed periods](ADAPTERS.md#feed-periods)).

`LiquidityUniverse(..., require_fundamentals=True)` keeps only securities the
provider has company fundamental data for, applied before the count is taken. In
practice it drops funds and notes, which otherwise rank among the most liquid
securities. A provider that cannot say reports the universe as not ready.
Membership holds each security once, even when the provider's selection data
lists it twice.

`FundamentalUniverse(name, rule)` selects with a rule over the provider's daily
fundamental records: `rule(at, records)` receives the selection time and one
`FundamentalRecord` per security (CIK, market value, traded price, dollar volume)
and returns the instruments to hold. The rule may read other point-in-time sources
the strategy owns, such as a list of recent SEC filers; it must use nothing dated
after `at`, and it returns `InstrumentId` values. A rule that cannot decide raises
`NotReady`, which leaves the universe unready. Definitions compare by name, `version`
and settings, not by the rule object; change `version` when the rule's meaning
changes. CIKs are stored without leading zeros.

`ctx.market.fundamentals(req, instrument)` returns the security's latest
`FundamentalRecord` known at graph time, and `ctx.market.short_availability(req,
instrument)` its latest `ShortAvailability` (annual fee rate, shortable quantity).
A missing, future-dated or (for fundamentals) older-than-`max_age` record raises
`NotReady`. The price in a fundamental record is the traded price of its date; use it,
or raw bars, for price levels (see [corporate actions](ADAPTERS.md#corporate-actions-and-data-resets)).

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

Bar prices may be zero or negative (some futures and back-adjusted series are);
a node that divides by a price checks its sign itself.

Readiness checks the filtered history count and maximum age. Excluding fill-forward
bars may leave too little history. Stale or missing data raise `NotReady`;
unsupported normalization or undeclared reads raise `CapabilityError`.
Normalization and return conventions are explicit assumptions, not cosmetic options.

**Partial universes.** By default a node is withdrawn unless every selected
instrument is ready, so a universe is never silently shrunk. A cross-sectional
node over a changing universe usually wants the opposite: a stock listed last
week lacks history and should simply be left out. Declare the requirement with
`require_all=False`; the node is then evaluated regardless and calls
`ctx.market.ready(requirement)` for the instruments it can read:

```python
bars = BarsRequirement(Selection(universe="liquid"), lookback=252, require_all=False)
...
for instrument in ctx.market.ready(bars):
    history = ctx.market.bars(bars, instrument)
```

An unavailable universe still withdraws the node, and what a partial selection
means (rank the rest, wait, or hold) is the node's explicit decision.

## Context services

`Context` binds lazy reads to one provider version and batch:

| Attribute | Use |
| --- | --- |
| `market` | Bars, quotes, indicators, ready members, membership and exact anchors |
| `instruments` | Metadata, tradability, session calendars and `session_close` |
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
account-wide NAV/cash.

`ctx.compute(key, inputs, parameters, version, function)` shares a hashable
calculation within the provider snapshot. It is not persistent model storage.
Provider mutation or Context closure invalidates reads with `SnapshotError`.
The runtime closes evaluation Contexts after processing; do not retain them.

## Units and exact anchors

Returns are decimal simple returns; horizons use aware intervals and ACT/365
calendar years. A horizon can be measured in trading sessions instead of elapsed
time: `ctx.instruments.session_close(instrument, after, count)` returns the close
of the count-th regular session ending strictly after `after`. One session after
a Friday close ends at Monday's close, where a price exists and the market is
open; one calendar day after it ends on Saturday. Quantities are instrument units. Weights are account-NAV fractions.
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
