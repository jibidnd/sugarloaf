# Memory adapter

`MemoryAdapter` implements the adapter interface for tests and examples. The
caller drives it: `deliver` hands it bars, quotes, custom records and lifecycle records at a
time; `fire` fires a schedule; `set_members` and `refresh` change a universe's membership.
The adapter consolidates, keeps windows, updates its indicators, reports the account,
sizes orders and fills them at the price last given, and calls the runner as a platform
would.

## Entry points

- [`MemoryAdapter`](adapter.py): `attach(runner)`, `deliver(...)`, `fire(name, time)`,
  `set_members(universe, members)`, `refresh(universe, rows, at)`, `set_price(instrument,
  price)`, `fill(order_id)`; the adapter interface methods.
- [`Sessions`](adapter.py): an exchange calendar given as a tuple of `(open, close)` pairs.
- [Indicator formulas](indicators.py): SMA, EMA, Wilder's RSI and Donchian computed in Python
  from the bars the adapter was handed; LEAN computes its own in a backtest.
- [Complete example](../../../examples/memory.py).

```powershell
uv run --cache-dir .uv-cache python -m examples.memory
```

## Behaviour

A delivered bar goes to every subscribed feed of its instrument whose resolution period
equals the bar's duration. An indicator updates from completed bars of a feed with the same
key as `Indicator.bars`, so a node declares the matching `Bars` input. With `fills=True`
(default) every order fills immediately at the current mark; with `fills=False` orders stay
open until `fill(order_id)`. There is no slippage, no fee and no calendar unless one is
supplied.

The full description is in [adapters](../../../docs/technical/ADAPTERS.md#memory). Tests:
[memory adapter](../../../tests/test_memory_adapter.py), [runner](../../../tests/test_runner.py).
