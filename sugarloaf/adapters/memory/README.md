# Memory adapter

The memory adapter supplies explicit observations, account state and calendars for
small deterministic runs and regression tests. It is useful for tracing framework
behavior without QC. It does not simulate markets, discover historical data or
calculate native indicators.

## Entry points

- [`MemoryServices`](services.py): fixture writes, lazy guarded reads, account,
  calendar, proportional costs and fixture buying-power/borrow checks.
- [`MemoryGateway`](gateway.py): records intents, explicit fills and cancellation
  requests/confirmations; feeds cumulative order updates to execution.
- [Complete example](../../../examples/memory.py): net two branches, submit once,
  apply a partial fill, replay the batch and complete the fill.

```powershell
uv run --cache-dir .uv-cache python -m examples.memory
```

Supply instruments, prices/account NAV and session intervals when executing orders.
`put_bars`, `put_quotes`, `put_series` and `set_selection` change fixture state;
Context visibility still obeys observation and availability times.
`set_account` invalidates older Contexts. Order fills occur only when the caller
explicitly applies them. A cancel request alone does not release exposure.

## Design and limits

`MemoryServices` extends `ProviderServices`, sharing declaration guards, resource
ownership, readiness and snapshots with other providers. Its buffers contain neutral
immutable observations. Schedules need explicit session fixtures; non-UTC named
timezones depend on installed timezone data. Fixture buying power is a NAV bound
with reducing-order allowance, not a brokerage margin model.

See [market contracts](../../../docs/technical/MARKET.md),
[provider lifecycle](../../../docs/technical/ADAPTERS.md#components-and-lifecycle)
and [execution](../../../docs/technical/EXECUTION.md) for the full contracts.
[Memory tests](../../../tests/test_adapter_memory.py) and
[execution tests](../../../tests/test_execution.py) show controlled failure cases.
