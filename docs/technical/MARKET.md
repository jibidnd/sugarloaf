# Market data and Context

Nodes declare what they may read; providers implement those reads. Requirements
describe data and service needs. Triggers separately decide when to evaluate.
The neutral contracts live in [market/contracts.py](../../sugarloaf/market/contracts.py);
service protocols and Context live in [core/services.py](../../sugarloaf/core/services.py).

## Requirements

| Requirement | Meaning |
| --- | --- |
| `BarsRequirement` | Selection, interval, lookback, session, normalization, fill-forward, maximum age and `require_all` |
| `QuotesRequirement` | Bid/ask observations, interval, lookback, session, normalization (default `raw`) and freshness |
| `IndicatorRequirement` | Native indicator over a bar source, immutable parameters and valid output window |
| `UniverseRequirement` | Named liquidity, historical ETF-constituent or fundamental-rule definition |
| `FundamentalsRequirement` | The selected securities' latest fundamental records, with a maximum age and the generic fields the node reads |
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
`FundamentalRecord` per security and returns the instruments to hold. The rule may read other point-in-time sources
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

### Fundamental fields

A `FundamentalRecord` always has its typed fields: CIK, market value, traded price,
dollar volume, `morningstar_sector_code` (three digits, for example 311 technology,
104 real estate) and `is_reit`. Anything else the provider has is read by name, and
only when declared: `FundamentalsRequirement(..., fields=[...])` and
`FundamentalUniverse(..., fields=[...])` list the names, and the records they read
carry exactly those in `record.fields`; `record.value(name)` refuses an undeclared
name. A name is the provider's qualified path, for example
`morningstar.valuation_ratios.pe_ratio`. A multi-period value names its period,
`morningstar.operation_ratios.roe.one_year`; there is no default period.

```python
pe = "morningstar.valuation_ratios.pe_ratio"
roe = "morningstar.operation_ratios.roe.one_year"

def cheap_and_profitable(at, rows):
    # Each row carries exactly the declared fields; a value the provider lacks is None.
    return [r.instrument for r in rows
            if r.morningstar_sector_code == 311 and not r.is_reit
            and r.value(pe) is not None and r.value(pe) < 20
            and r.value(roe) is not None and r.value(roe) > 0.15]

universe = FundamentalUniverse("cheap-tech", cheap_and_profitable, fields=[pe, roe])
```

Values are a float, int, str, bool, `date` or None. A real zero or False is kept;
a supported value the provider does not have is None. Field lists are stored sorted
and without repeats and are part of a definition: one universe name with two field
lists is a conflict. An unknown name fails when the graph is installed. `require_all`
still means a record exists, not that its fields are filled, and `max_age` measures
the record's read time, not a filing date (a provider's own dates are fields).

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
| `market` | Bars, quotes, indicators, ready members, membership and anchor prices |
| `instruments` | Metadata, tradability, session calendars, `session_close` and `session_open` |
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
open; one calendar day after it ends on Saturday.
`ctx.instruments.session_open(instrument, after, count)` is its counterpart for
intervals measured from an open: the open of the count-th regular session
starting strictly after `after` (at a Friday close, count 1 is Monday's open). Quantities are instrument units. Weights are account-NAV fractions.
Unit valuation is mark × multiplier × account-currency FX; unsupported conversion
fails rather than guessing.

`ctx.market.anchor(instrument, at, convention)` returns the instrument's price at
`at` from declared convention-compatible bar history; it fetches no new data.
Sugarloaf keeps no clock of its own: the clock moves when data arrives, from any
instrument. So the price at `at` is read at the first clock tick at or after `at`
(the first admitted batch time), and treated by the instrument's session, the one
containing that tick or the latest before it (regular or extended hours, as the
bars requirement declares):

1. A bar of the instrument ends at the tick: its close.
2. No trade at the tick, but one earlier in that session: the last trade stands,
   so the close of the instrument's last bar before the tick. This includes a
   tick after the close and before the next open. A bar the provider repeated
   because nothing traded (`fill_forward`) is not a trade.
3. No trade yet in a session still open at the tick, for example just after the
   open: the previous session's price is not used, because it would put the move
   between sessions into the wrong interval. The price is the instrument's first
   trade in the session, available when that bar arrives.
4. No trade in the session at all: missing data, `NotReady`.

For example, with minute bars an interval starting at a 09:30 open takes the
09:31 close of a stock that traded in the first minute, and the 09:34 close of
one whose first trade came then. A tick not yet reached, a first trade not yet
arrived, and readable history that does not reach back to the session open (so
an earlier trade cannot be ruled out) all raise `NotReady`. Outcome resolution is
separate and may use its own anchor source. See
[research outcomes](RESEARCH.md#outcomes-and-extensions).

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
