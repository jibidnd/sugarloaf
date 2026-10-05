# Deployment examples

Shared configuration, evidence and reporting live in
`sugarloaf.deployment`. QC packaging, backtests and notebook execution live in
`sugarloaf.adapters.quantconnect.cloud`; the QC launcher lives in the adapter.
This folder contains public teaching deployment definitions. Private strategy
composition belongs in an external package.

- [Public EMA manifest](quantconnect/ema.toml) and [factory](quantconnect/config.py)
- [External strategy workflow](../docs/EXTERNAL_STRATEGIES.md)
- [Deploy and inspect](../docs/GUIDE.md#4-deploy-and-inspect)

Run `sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml` to inspect the
EMA bundle offline. In an authenticated LEAN environment, use
`sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml` to upload, compile,
backtest and retain results/logs. Select an existing project or run
`sugarloaf create --adapter quantconnect --manifest deployments/quantconnect/ema.toml` once first.
Run `python -m sugarloaf.adapters.quantconnect.cloud.notebooks NOTEBOOK --project-id ID` to execute a
local notebook on QC's remote kernel using an already-authenticated LEAN environment.
