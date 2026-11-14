# QuantConnect adapter review diagnostics

Scripts and results behind
[the adapter review of 14 November 2026](../../docs/technical/quantconnect-adapter-review-2026-11-14.md).
They measure, on QuantConnect, what the move-size node's declarations cost through the
adapter and what the same reads cost through LEAN's own calls. They change no adapter code.

| File | What it runs |
| --- | --- |
| [native_history.py](native_history.py), [native_history.toml](native_history.toml) | Plain LEAN, no Sugarloaf: the 214-security panel's 4,900-bar daily history in four shapes, the earnings calendar two ways, indicator warm-up, fundamentals and corporate-action history; one task per session |
| [native_warmup.py](native_warmup.py), [native_warmup.toml](native_warmup.toml) | Plain LEAN: `set_warm_up(4900, Resolution.DAILY)` over the same securities, and when a `train` callback requested from `on_data` runs |
| [adapter_probe.py](adapter_probe.py), [adapter_probe.toml](adapter_probe.toml) | A Sugarloaf deployment whose node declares what the move-size node declares and reads it once a session; the host is profiled with `cProfile` |
| [collect.py](collect.py) | Copies one run's identity, statistics and diagnostic records from the git-ignored `artifacts/` folder into [results/](results/) |
| [local_bench.py](local_bench.py) | Local timings of the adapter's own Python (admission, dedupe scan, read pass, `Bar` construction) against the tests' fake LEAN boundary |

All three manifests use QuantConnect project 39378262, created for this review. Run one
from the repository root with QC credentials:

```powershell
uv run --cache-dir .uv-cache sugarloaf run --adapter quantconnect --manifest tools/qc_adapter_review/native_history.toml --name "native history cost"
uv run --cache-dir .uv-cache python tools/qc_adapter_review/collect.py native_history BACKTEST_ID
```

Times are wall-clock seconds on the backtest node named in each result file; compare runs
on the same node class only. The results folder holds the runs the review cites:

| Result file | Run |
| --- | --- |
| `native_history-22bfc237…json` | D1: plain LEAN, the panel's reads, one task per session |
| `native_warmup-c251b818…json` | D2: warm-up of 4,900 daily bars for 215 securities; `train` timing; typed earnings shapes for one year |
| `native_warmup-512edc41…json` | D2b: the same with the earnings calendar streamed through warm-up; the 19-year typed earnings read |
| `adapter_probe-4ceff346…json` | D3: the adapter probe with `cProfile` on the host (breakdown) |
| `adapter_probe-1a4e11b1…json` | D3 unprofiled (wall time) |
