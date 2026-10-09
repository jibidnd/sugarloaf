# Technical strategy examples

These are portable, direct-position nodes. They publish positions without a predictive aggregation model.
They use adjusted completed bars, fixed instruments and long/cash positions.
NumPy validates histories; SciPy evaluates the explicit smoothing recurrences.

These batch-reference teaching fixtures use portable calculations. For the
QuantBook single-node research path,
use [EMAScore](../EMA_RESEARCH.md); its calculations and rolling windows belong
to QuantConnect. The batch routines here are not a provider fallback.

| Node | Default rule | Motivation and main weakness |
| --- | --- | --- |
| `EMACrossover` | Hold 10% NAV per instrument while EMA(20) > EMA(50); otherwise zero | Follow persistent trends; sideways markets can whipsaw |
| `RSIMeanReversion` | Enter at Wilder RSI(14) <= 30; exit at >= 50; hold between thresholds | Bet on recovery after weakness; sustained declines can keep losing |
| `DonchianBreakout` | Enter when close exceeds previous 20 bars' highs; exit below previous 10 bars' lows | Follow price breakouts; false breakouts and delayed exits hurt |

`weight` is per instrument, not divided among active instruments. Unused allocation
stays cash. EMA uses the current fast/slow regime and can enter immediately after
warm-up; it does not require a newly observed crossing. Donchian excludes the
current bar from channel construction. Including it would make a close above
that bar's own high impossible.

Each constructor accepts `weight`, `scope`, `interval` (default one day), and
`lifetime` (default four calendar days). EMA/RSI also accept `history`. The example
freshness policy allows bars up to four days old; adjust the declared requirement
for a different market/session policy. Use a fresh node instance for each run.

## Run and read

```powershell
uv run --cache-dir .uv-cache python -m examples.technical.memory ema
uv run --cache-dir .uv-cache python -m examples.technical.memory rsi
uv run --cache-dir .uv-cache python -m examples.technical.memory donchian
```

Read `nodes.py` for decisions, `indicators.py` for numerical conventions, then
`memory.py` for the adapter/runtime/execution assembly. Output shows signal
transitions, order count, final quantity and how many orders replay added (zero).
The synthetic path falls, rises, then falls again. The trend rules finish flat;
RSI re-enters on the last decline. Weight maintenance can submit orders between
signal transitions because prices and NAV change.

The fixture invents daily sessions and supplies immediate fills at its current
mark. It omits realistic fees, spreads and exchange-calendar effects. It verifies
plumbing, not investment performance. These batch examples have local fixture validation only. They have no
held-out predictive study or native-indicator equivalence claim.

```python
from examples.technical import EMACrossover, build_graph
from sugarloaf.core.primitives import InstrumentId

spy = InstrumentId("USA:SPY")
node = EMACrossover((spy,), fast=20, slow=50, weight=0.25)
graph = build_graph(node)
```

`build_graph` connects the signal to a `PortfolioConstraintNode`. A QC host can
receive this same graph and the identity/ticker mapping. Its daily signals become
available at the session close; regular-session execution may wait until the next
open. The source adapter must support the requested adjusted data and warm-up.
To combine strategies, use separate instances/scopes and an explicit
`PositionNetter`, then account constraints; choose branch budgets deliberately.

## Indicator semantics

The examples are batch reference implementations, recalculated over the declared
history at each evaluation. EMA seeds with the mean of the first N supplied
closes, then uses alpha=2/(N+1). RSI seeds mean gains/losses over N changes, then
uses Wilder smoothing alpha=1/N. SciPy `signal.lfilter` implements the recurrence.
RSI requires N+1 prices. A completely flat window returns 50; all-up returns 100
and all-down returns 0. Flat-window conventions can differ across providers.

EMA requests 8 times the slow period of history; RSI requests 12 times its period
plus one. These reduce seed sensitivity but are not a promise of equality with
an indefinitely maintained streaming indicator. A rolling window changes its
seed as old bars drop out. `history=` is an explicit strategy parameter, not just
a performance setting. Provider comparisons must use identical input bars,
seed, recurrence, normalization, timestamps and history policy.

The node's `_long` dictionary records signal intent for RSI/Donchian hysteresis.
It is not broker holdings. A rejected or partial order does not rewrite the
signal. Reads for all instruments succeed before this state changes. The graph
runtime skips replayed batches. A new node instance starts flat; continuing a live
deployment would require state persistence or deterministic reconstruction.

`ctx.compute` avoids duplicate calculations within one snapshot. It is not an
indicator database and cannot preserve recursive state across batches.

The QC-native indicator contract is described in
[`docs/technical/ADAPTERS.md`](../../docs/technical/ADAPTERS.md).
Its provider defaults and forward-only update behavior are distinct from these
batch references; equality across implementations is not promised.

## Sources and evidence

These are repository-authored illustrations of conventional trend-following and
contrarian rules, not replications of a published trading experiment. EMA is
exponential smoothing; RSI uses Wilder's gain/loss smoothing. The period choices,
entry/exit thresholds and position sizes above are unvalidated teaching choices.
Official implementation references for the native provider are QuantConnect's
[EMA](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/exponential-moving-average),
[RSI](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/relative-strength-index)
and [Donchian channel](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/donchian-channel)
documentation. These links describe the provider, not a claim that the batch
formulas here exactly match it.

[Focused tests](../../tests/test_technical_examples.py) check seeding, flat/up/down
RSI boundaries, hysteresis, previous-bar Donchian channels, explicit exits and
orders/fills/replay for all three fixtures. The synthetic decline/rally/decline
path exposes the expected final distinction: trend rules exit, while the
contrarian rule re-enters a falling market. This confirms programmed behavior,
not forecast quality. No held-out predictive evaluation, parameter selection
study or realistic costed backtest supports these examples as alpha.
