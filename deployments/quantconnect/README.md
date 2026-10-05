# QuantConnect deployment example

[ema.toml](ema.toml) explicitly selects the public EMA graph and [factory](config.py).
The factory returns `sugarloaf.deployment.Deployment`. The adapter launcher applies
its environment; the adapter's generic algorithm entrypoint forwards callbacks.
A generated `deployment_config.py` binds the selected factory inside each uploaded
bundle.

Private packages provide their own factory and manifest without changing this
example or adding a framework registry entry. See [external strategies](../../docs/EXTERNAL_STRATEGIES.md).
Each TOML field has inline comments; the [field reference](../../docs/EXTERNAL_STRATEGIES.md#manifest-fields)
lists requirements, defaults and accepted values. The adjacent `config.py` is
the example's factory implementation; Sugarloaf's `deployment/config.py` defines
the shared objects that factory returns.

From the repository root, `sugarloaf package --adapter quantconnect --manifest deployments/quantconnect/ema.toml`
builds the bundle offline. With an authenticated LEAN environment and a selected
QC project, `sugarloaf run --adapter quantconnect --manifest deployments/quantconnect/ema.toml` uploads,
compiles, waits for the backtest and retains results/logs under `artifacts/ema/`.
If no project is selected in the manifest or saved state, use
`sugarloaf create --adapter quantconnect --manifest deployments/quantconnect/ema.toml` once.

The factory chooses which parameter names it consumes. Supply temporary overrides
with repeated `--parameter NAME VALUE`; edit `[parameters]` to keep defaults in
the manifest. See [commands and evidence](../../docs/technical/DEPLOYMENT.md).
