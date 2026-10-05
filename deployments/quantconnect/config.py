"""Public EMA teaching deployment; private strategies provide their own factories."""

# Durations separately specify source cadence and the score's calendar forecast horizon.
from datetime import timedelta

# This deployment composes the public teaching graph; private factories select their own nodes.
from examples.ema import EMAConfig
from examples.ema import build_graph as ema_graph
from sugarloaf.core.types import InstrumentId
from sugarloaf.deployment import Deployment
from sugarloaf.execution import ExecutionPolicy


def ema_definition(parameter):
    # Consume only the parameter names this factory understands, keeping a local default.
    ticker = parameter("ticker") or "SPY"
    instrument = InstrumentId(f"USA:{ticker}")
    # Periods in EMAConfig count five-minute bars; horizon does not change those periods.
    config = EMAConfig(interval=timedelta(minutes=5), horizon=timedelta(days=1))
    return Deployment(
        "ema",
        config,
        # A factory gives each run fresh node state; the adapter creates native provider state.
        lambda: ema_graph(instrument, config),
        {instrument: ticker},
        # Initial stable identity/ticker binding lets the adapter select the native security.
        # Other environment and safety fields use Deployment's declared defaults.
        execution=ExecutionPolicy(min_trade_value=1000),
        # The minimum is account-currency notional, reducing insignificant maintenance orders.
    )
