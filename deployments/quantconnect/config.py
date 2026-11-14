"""Public EMA teaching deployments; private strategies provide their own factories."""

from datetime import timedelta

from examples.ema import EMAConfig, build_forecast_graph, build_graph
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.deployment import Deployment
from sugarloaf.execution import ExecutionPolicy


def _config(parameter):
    # Consume only the parameter names this factory understands, keeping local defaults.
    ticker = parameter("ticker") or "SPY"
    minutes = int(parameter("interval_minutes") or "5")
    return InstrumentId(f"USA:{ticker}"), ticker, EMAConfig(
        resolution="minute", interval=timedelta(minutes=minutes), horizon=timedelta(days=1)
    )


def ema_definition(parameter):
    """The trading graph: score, long/cash allocation, account check."""
    instrument, ticker, config = _config(parameter)
    return Deployment(
        "ema",
        config,
        # A factory gives each run fresh node state.
        lambda: build_graph(instrument, config),
        {instrument: ticker},
        execution=ExecutionPolicy(min_trade_value=1000),
    )


def ema_forecasts(parameter):
    """The score alone: a forecast run that archives its views for research."""
    instrument, ticker, config = _config(parameter)
    return Deployment(
        "ema-forecasts", config, lambda: build_forecast_graph(instrument, config),
        {instrument: ticker},
    )
