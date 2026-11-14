"""What a node declares it needs: data subscriptions, indicators, schedules and warm-up.

A node lists these declarations as class or instance attributes. The runner reads
them once, before the platform starts, and asks the adapter to set each one up.
Nothing here fetches data: a declaration is a request the adapter translates into
the platform's own calls (a subscription, a consolidator, an indicator, a scheduled
event). The shapes follow QuantConnect's algorithm API so that a node reads like an
alpha model, but they name no platform type.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, finite

if TYPE_CHECKING:
    from sugarloaf.core.data import FundamentalRecord

# Feed resolutions the adapters deliver. A coarser bar is built from a finer feed
# by the platform's consolidator when a declaration asks for an ``interval``.
RESOLUTIONS = ("minute", "hour", "daily")
RESOLUTION_PERIODS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "daily": timedelta(days=1),
}
# "adjusted" prices are corrected for splits and dividends, "split_adjusted" for
# splits only, "total_return" reinvests dividends, "raw" is as traded.
NORMALIZATIONS = ("raw", "adjusted", "split_adjusted", "total_return")
CADENCES = ("daily", "weekly", "monthly", "yearly")
# Cadence period of a selection time, by the local date of the selection.
_PERIODS = {
    "daily": lambda day: (day,),
    "weekly": lambda day: tuple(day.isocalendar())[:2],
    "monthly": lambda day: (day.year, day.month),
    "yearly": lambda day: (day.year,),
}


@dataclass(frozen=True)
class Selection:
    """Explicit instruments or one named universe, never both.

    A universe name refers to a ``Universe`` declared by this node or another node of
    the graph; its members at any time are whatever the platform selected last.
    """

    instruments: tuple[InstrumentId, ...] = ()
    universe: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "instruments", tuple(self.instruments))
        if self.instruments and self.universe is not None:
            raise ContractError("Select explicit instruments or one universe")
        if len(set(self.instruments)) != len(self.instruments):
            raise ContractError("Duplicate selected instrument")
        if not all(isinstance(i, InstrumentId) for i in self.instruments):
            raise ContractError("Selections name InstrumentId values")


def _check_interval(resolution: str, interval: timedelta | None) -> None:
    if resolution not in RESOLUTIONS:
        raise ContractError(f"Resolution must be one of {', '.join(RESOLUTIONS)}")
    if interval is None:
        return
    period = RESOLUTION_PERIODS[resolution]
    if interval <= period or interval % period != timedelta(0) or interval > timedelta(days=1):
        raise ContractError(
            "A consolidation interval is a whole multiple of the feed period, above it, "
            "and at most one day"
        )


class Input:
    """Marker for the declarations that put items into a node's slice."""


@dataclass(frozen=True)
class Bars(Input):
    """Trade bars for the selected instruments.

    Attributes:
        selection: Explicit instruments or a universe name.
        resolution: The feed the platform subscribes: minute, hour or daily.
        interval: Consolidate the feed to this period (``timedelta(minutes=5)``);
            None delivers the feed's own bars. Daily from an intraday feed means
            the exchange session.
        window: How many completed bars the platform keeps for ``ctx.window``.
            The newest bar is index 0, as in a rolling window.
        normalization: Price adjustment convention.
        extended_hours: Include pre- and post-market trading.
        fill_forward: Let the platform repeat the last bar when nothing traded.
        name: Optional label for ``data.series(name)`` when a node declares two
            bar inputs for the same instrument at different intervals.

    Two declarations with equal ``key`` share one subscription and one consolidator.
    """

    selection: Selection
    resolution: str = "daily"
    interval: timedelta | None = None
    window: int = 1
    normalization: str = "adjusted"
    extended_hours: bool = False
    fill_forward: bool = False
    name: str | None = None

    def __post_init__(self):
        _check_interval(self.resolution, self.interval)
        if self.window < 1:
            raise ContractError("A bars window keeps at least one bar")
        if self.normalization not in NORMALIZATIONS:
            raise ContractError("Unknown normalization")

    @property
    def key(self) -> tuple:
        """Identity of the backing feed and consolidation, without window or name."""
        return (
            "bars",
            self.resolution,
            self.interval,
            self.normalization,
            self.extended_hours,
            self.fill_forward,
        )

    @property
    def period(self) -> timedelta:
        """Duration of one delivered bar."""
        return self.interval or RESOLUTION_PERIODS[self.resolution]


@dataclass(frozen=True)
class Quotes(Input):
    """Bid/ask bars for the selected instruments; as traded, never adjusted."""

    selection: Selection
    resolution: str = "minute"
    interval: timedelta | None = None
    window: int = 1
    extended_hours: bool = False
    name: str | None = None

    def __post_init__(self):
        _check_interval(self.resolution, self.interval)
        if self.window < 1:
            raise ContractError("A quotes window keeps at least one bar")

    @property
    def key(self) -> tuple:
        return ("quotes", self.resolution, self.interval, self.extended_hours)

    @property
    def period(self) -> timedelta:
        return self.interval or RESOLUTION_PERIODS[self.resolution]


CUSTOM_KINDS = ("earnings", "fundamentals", "borrow")


@dataclass(frozen=True)
class Custom(Input):
    """A platform dataset by neutral name, delivered in ``data.custom[kind]``.

    ``earnings``: the earnings calendar's daily listings (report date and timing).
    ``fundamentals``: each selected security's latest fundamental record.
    ``borrow``: each selected security's borrow fee and shortable quantity.
    """

    kind: str
    selection: Selection = Selection()

    def __post_init__(self):
        if self.kind not in CUSTOM_KINDS:
            raise ContractError(f"Custom data kind must be one of {', '.join(CUSTOM_KINDS)}")


@dataclass(frozen=True)
class Universe(Input):
    """A named membership rule the platform evaluates on its own schedule.

    Attributes:
        name: The name other declarations refer to through ``Selection(universe=name)``.
        cadence: How often membership may change: daily (the platform's own
            refresh), weekly, monthly or yearly. Between refreshes it stands.
        minimum_residence: How long a member stays after dropping out of the rule.
    """

    name: str
    cadence: str = field(default="daily", kw_only=True)
    minimum_residence: timedelta = field(default=timedelta(0), kw_only=True)

    def __post_init__(self):
        if type(self) is Universe:
            raise ContractError("Declare a specific universe, such as LiquidityUniverse")
        if not self.name or self.minimum_residence < timedelta(0):
            raise ContractError("Invalid universe")
        if self.cadence not in CADENCES:
            raise ContractError(f"Universe cadence must be one of {', '.join(CADENCES)}")

    def period(self, at: datetime) -> tuple:
        """The cadence period containing a selection time, by its date."""
        return _PERIODS[self.cadence](at.date())


@dataclass(frozen=True)
class LiquidityUniverse(Universe):
    """The ``count`` securities with the largest dollar volume that pass the filters."""

    count: int
    min_price: float = 0
    min_volume: float = 0
    # Keep only securities the platform has company fundamentals for, which
    # drops funds and notes from a list of the most liquid "stocks".
    require_fundamentals: bool = False

    def __post_init__(self):
        super().__post_init__()
        finite(self.min_price, self.min_volume)
        if self.count < 0 or min(self.min_price, self.min_volume) < 0:
            raise ContractError("Invalid liquidity selection")


@dataclass(frozen=True)
class ETFConstituentsUniverse(Universe):
    """The constituents of an ETF, by weight."""

    etf: InstrumentId
    count: int | None = None
    min_weight: float | None = None
    # A constituent whose weight the source omits: "exclude" it, "include" it
    # (ranked last), or "error" (the selection is not made).
    missing_weight: str = "exclude"

    def __post_init__(self):
        super().__post_init__()
        if self.count is not None and self.count < 0:
            raise ContractError("Invalid ETF selection")
        if self.min_weight is not None:
            finite(self.min_weight)
            if self.min_weight < 0:
                raise ContractError("Minimum constituent weight must be nonnegative")
        if self.missing_weight not in {"exclude", "include", "error"}:
            raise ContractError("Unknown missing-weight policy")


@dataclass(frozen=True)
class FundamentalUniverse(Universe):
    """Members chosen by ``rule(at, records)`` over the platform's fundamental records.

    The rule receives the selection time and one ``FundamentalRecord`` per security
    and returns the instruments to hold. It must use only what is known at ``at``.
    Definitions compare by name, version and settings, not by the rule object.
    """

    rule: Callable[[datetime, tuple["FundamentalRecord", ...]], Iterable[InstrumentId]] = field(
        compare=False
    )
    version: str = "1"

    def __post_init__(self):
        super().__post_init__()
        if not callable(self.rule):
            raise ContractError("Invalid fundamental selection")


INDICATOR_KINDS = ("sma", "ema", "rsi", "donchian")


@dataclass(frozen=True)
class Indicator:
    """A platform-native indicator per selected instrument, read through ``ctx.indicator``.

    Attributes:
        name: The name the node reads it by.
        kind: ``sma``, ``ema``, ``rsi`` or ``donchian``.
        period: Indicator period in bars.
        selection: Instruments or a universe.
        resolution: Feed of the bars the indicator consumes.
        interval: Consolidation of that feed, as for ``Bars``.
        window: How many past values the platform keeps.
        field: Bar field fed to a scalar indicator: open, high, low, close or volume.
        normalization: Price adjustment of the bars fed to it.
        parameters: Extra settings by name, such as ``("moving_average_type", "wilders")``.

    The platform warms the indicator from history before the first slice.
    """

    name: str
    kind: str
    period: int
    selection: Selection
    resolution: str = "daily"
    interval: timedelta | None = None
    window: int = 1
    field: str = "close"
    normalization: str = "adjusted"
    parameters: tuple[tuple[str, int | float | str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))
        if not self.name or self.kind not in INDICATOR_KINDS:
            raise ContractError(f"Indicator kind must be one of {', '.join(INDICATOR_KINDS)}")
        if isinstance(self.period, bool) or not isinstance(self.period, int) or self.period < 1:
            raise ContractError("Indicator period must be a positive integer")
        _check_interval(self.resolution, self.interval)
        if self.window < 1:
            raise ContractError("An indicator window keeps at least one value")
        if self.field not in {"open", "high", "low", "close", "volume"}:
            raise ContractError("Unsupported indicator input field")
        if self.normalization not in NORMALIZATIONS:
            raise ContractError("Unknown normalization")
        if len(dict(self.parameters)) != len(self.parameters):
            raise ContractError("Duplicate indicator parameter")

    @property
    def bars(self) -> Bars:
        """The bar feed this indicator consumes."""
        return Bars(
            self.selection, self.resolution, self.interval, normalization=self.normalization
        )


@dataclass(frozen=True)
class Schedule:
    """A named callback time, delivered to ``on_scheduled(ctx, name, views)``.

    Attributes:
        name: The name passed to the callback.
        calendar: The instrument whose exchange calendar defines trading days and
            session times. Required for ``after_open`` and ``before_close``.
        days: ``every_day`` (each trading day of the calendar), ``week_start``,
            ``month_start``, ``month_end`` or ``year_start``.
        after_open: Minutes after the session open.
        before_close: Minutes before the session close.
        at: A wall time in ``timezone`` instead of a session anchor.
        timezone: Zone of ``at``.

    Exactly one of ``after_open``, ``before_close`` and ``at`` is set. A scheduled
    time fires when the platform's clock reaches it, which in a backtest is the
    next data time step at or after that time.
    """

    name: str
    calendar: InstrumentId | None = None
    days: str = "every_day"
    after_open: int | None = None
    before_close: int | None = None
    at: time | None = None
    timezone: str = "America/New_York"

    def __post_init__(self):
        anchors = [x for x in (self.after_open, self.before_close, self.at) if x is not None]
        if not self.name or len(anchors) != 1:
            raise ContractError("A schedule names one time: after_open, before_close or at")
        if self.days not in {"every_day", "week_start", "month_start", "month_end", "year_start"}:
            raise ContractError("Unknown schedule day rule")
        if self.at is None and self.calendar is None:
            raise ContractError("Session-anchored schedules need a calendar instrument")
        if self.at is not None and self.at.tzinfo is not None:
            raise ContractError("Schedule wall time is naive, in the named timezone")


@dataclass(frozen=True)
class WarmUp:
    """History the platform replays through the graph before the first live slice.

    Either a bar count at a resolution or a calendar span. Nodes receive the
    replayed slices with ``ctx.warming_up`` set, and the runner records no views
    from them, so windows and node state fill without decisions.
    """

    count: int | None = None
    resolution: str = "daily"
    span: timedelta | None = None

    def __post_init__(self):
        if (self.count is None) == (self.span is None):
            raise ContractError("Warm up by a bar count or by a span, not both")
        if self.count is not None and self.count < 1:
            raise ContractError("Warm-up count must be positive")
        if self.span is not None and self.span <= timedelta(0):
            raise ContractError("Warm-up span must be positive")
        if self.resolution not in RESOLUTIONS:
            raise ContractError("Unknown warm-up resolution")


@dataclass(frozen=True)
class HistoryRequest:
    """A one-off read of past bars through ``ctx.history``.

    Allowed, not the norm: a refit, a universe refresh, a new member's lookback.
    """

    instruments: tuple[InstrumentId, ...]
    count: int | None = None
    span: timedelta | None = None
    resolution: str = "daily"
    interval: timedelta | None = None
    normalization: str = "adjusted"
    extended_hours: bool = False

    def __post_init__(self):
        object.__setattr__(self, "instruments", tuple(self.instruments))
        if not self.instruments:
            raise ContractError("A history request names at least one instrument")
        if (self.count is None) == (self.span is None):
            raise ContractError("Request history by a bar count or by a span, not both")
        if self.count is not None and self.count < 1:
            raise ContractError("History count must be positive")
        if self.span is not None and self.span <= timedelta(0):
            raise ContractError("History span must be positive")
        _check_interval(self.resolution, self.interval)
        if self.normalization not in NORMALIZATIONS:
            raise ContractError("Unknown normalization")
