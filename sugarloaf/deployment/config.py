"""Portable deployment definition; strategy packages supply the graph and settings."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from sugarloaf.core.errors import ContractError
from sugarloaf.core.graph import CompiledGraph
from sugarloaf.core.primitives import InstrumentId, finite
from sugarloaf.execution import ExecutionPolicy
from sugarloaf.portfolio.constraints import PortfolioConstraints

from .reporting import ReportContext


@dataclass(frozen=True)
class Environment:
    """Native backtest/account settings independent of strategy parameters.

    Cash is in account currency; slippage is a proportional notional rate.
    Dates are inclusive platform dates in the configured timezone. Brokerage
    and account_type name native enum members resolved by the launcher.
    """

    # These describe the account/backtest environment, not signal hyperparameters.
    # The adapter translates them into native calls when the run starts.
    start: date = date(2025, 1, 6)
    end: date = date(2025, 1, 10)
    cash: float = 100000
    currency: str = "USD"
    timezone: str = "America/New_York"
    benchmark: str = "SPY"
    brokerage: str = "DEFAULT"
    account_type: str = "MARGIN"
    slippage: float = 0.0005
    leverage: float = 1.0
    # Borrow data for short-availability reads: None for the platform's default, or
    # "interactive_brokers" for Interactive Brokers' fee and shortable-quantity history.
    borrow_data: str | None = None

    def __post_init__(self):
        # Reject nonsensical environment settings before any provider request or order.
        # Brokerage names and supported features are still checked at the adapter boundary.
        finite(self.cash, self.slippage, self.leverage)
        if self.end < self.start or self.cash <= 0 or self.slippage < 0 or self.leverage <= 0:
            raise ContractError("Invalid deployment environment settings")
        if self.borrow_data not in (None, "interactive_brokers"):
            raise ContractError("Unknown borrow data source")


@dataclass(frozen=True)
class Deployment:
    """Explicit strategy composition for the generic platform launcher.

    Attributes:
        name: Deployment identity used in diagnostic reports.
        strategy: Reportable strategy configuration supplied by the caller.
        graph_factory: Zero-argument factory returning fresh compiled node state.
        tickers: Initial stable instrument-to-ticker mappings.
        environment: Platform dates, account, brokerage, and execution-model settings.
        execution: Supported order settings and staleness limits.
        constraints: Execution safety limits, configured separately from graph limits.
        report: Optional end-of-run callback receiving a portable ReportContext.
            Inspect its runtime or emit labeled records; return summary diagnostics.
    """

    # A factory returns this definition; it does not start a process by constructing it.
    # The selected adapter calls graph_factory later to obtain fresh mutable node state.
    name: str
    strategy: object
    # Store a callable, not a shared compiled graph: successive runs must not reuse
    # monthly gates, indicator-dependent node state or prior forecast identities.
    graph_factory: Callable[[], CompiledGraph]
    # Seed known instrument identities (including universe anchors) with ticker names.
    # This is not a trading whitelist: native universes bind discovered members separately.
    tickers: Mapping[InstrumentId, str]
    environment: Environment = field(default_factory=Environment)
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    constraints: PortfolioConstraints = field(default_factory=PortfolioConstraints)
    # Reporting gets graph access and an adapter-owned record sink, never a native
    # algorithm/client. Returned diagnostics are included in the end-of-run summary.
    report: Callable[[ReportContext], object] | None = None
