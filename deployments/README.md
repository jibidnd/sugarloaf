# Deployment examples

Shared configuration, evidence and reporting live in `sugarloaf.deployment`. QuantConnect
packaging, backtests and notebook execution live in `sugarloaf.adapters.quantconnect.cloud`;
the launcher lives in the adapter. This folder holds public teaching deployments; private
strategies belong in an external package.

| Manifest | Factory | Run |
| --- | --- | --- |
| [ema.toml](quantconnect/ema.toml) | `ema_definition` in [config.py](quantconnect/config.py) | Trading: `EMAScore` → `LongWhenPositive` → `PortfolioConstraintNode` on SPY |
| [ema_forecasts.toml](quantconnect/ema_forecasts.toml) | `ema_forecasts` in [config.py](quantconnect/config.py) | Forecast run: `EMAScore` alone, archived to the Object Store |
| [index_check.toml](quantconnect/index_check.toml) | `deployment` in [index_check.py](quantconnect/index_check.py) | Forecast run: SPY and the VIX index read side by side, reported per session |

Build a bundle offline:

```powershell
sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml
```

With QuantConnect credentials (`QC_USER_ID` and `QC_API_TOKEN`, or a saved LEAN login) and a
selected project, `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml`
uploads, compiles, backtests and retains the results and logs. `sugarloaf create` with the
same arguments selects a new project once. `python -m sugarloaf.adapters.quantconnect.cloud.notebooks NOTEBOOK --project-id ID`
runs a local notebook on a remote research kernel.

See [deployment](../docs/technical/DEPLOYMENT.md),
[external strategies](../docs/EXTERNAL_STRATEGIES.md) and
[getting started](../docs/GUIDE.md#4-deploy-and-inspect).
