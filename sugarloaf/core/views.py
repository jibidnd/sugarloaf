"""Concrete strategy messages and structured predictive semantics.

Returns are decimal simple returns. Weights use account NAV, never branch NAV.
Distribution payloads own dependence and must implement immutable semantics.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from sugarloaf.core.errors import ContractError
from sugarloaf.core.types import InstrumentId, Interval, Metadata, aware, finite, frozen_map


@dataclass(frozen=True)
class Target:
    """Explicit subject, measured quantity, time horizon, units, and convention.

    Attributes:
        subject: Stable instrument, ordered joint instruments, or named subject.
        quantity: Claimed quantity, such as return or difference.
        time: Observation time, interval, or ordered joint intervals.
        units: Payload units; return Views require decimal simple returns.
        convention: Measurement rule, such as price_return or total_return.
        components: Two component targets when quantity is difference.

    A valid Target does not imply that every node or resolver supports its form.
    """
    # Describe exactly what is being predicted: subject, quantity, horizon and
    # measurement convention. Matching instrument alone is not enough to combine Views.
    subject: InstrumentId | tuple[InstrumentId, ...] | str
    quantity: str
    time: Interval | datetime | tuple[Interval, ...]
    units: str = "decimal"
    convention: str = "price_return"
    components: tuple["Target", ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "components", tuple(self.components))
        if not self.quantity or not self.units or not self.convention:
            raise ContractError("Target quantity, units and convention are required")
        if isinstance(self.subject, tuple) and not self.subject:
            raise ContractError("Empty joint target")
        if self.quantity == "difference" and len(self.components) != 2:
            raise ContractError("A difference target needs two components")
        if isinstance(self.time, datetime):
            aware(self.time)
        elif not isinstance(self.time, Interval) and not (
            isinstance(self.time, tuple)
            and self.time
            and all(isinstance(interval, Interval) for interval in self.time)
        ):
            raise ContractError("Target time requires an observation or explicit intervals")


def return_target(
    instrument: InstrumentId, interval: Interval, convention: str = "price_return"
) -> Target:
    """Construct a scalar decimal interval-return target.

    Args:
        instrument: Stable identity of the predicted instrument.
        interval: Exact forecast horizon.
        convention: Price-return or other explicitly supported measurement rule.
    """
    # Use this constructor for one instrument's decimal simple return. Relative
    # and joint targets carry additional structure rather than guessing from a score.
    return Target(instrument, "return", interval, convention=convention)


@dataclass(frozen=True)
class Estimate:
    """A finite point estimate with a positive forecast-error scale.

    The error scale describes uncertainty in the forecast, not asset volatility.
    Mean, variance, and quantile properties are representable; ReturnView and
    RelativeReturnView currently accept mean estimates only.
    """
    # For example, value=0.02 means a 2% simple return when used in a ReturnView.
    # The error scale reports forecast uncertainty; it is not automatically asset sigma.
    value: float
    forecast_error_scale: float
    property: str = "mean"
    quantile: float | None = None

    def __post_init__(self):
        finite(self.value, self.forecast_error_scale)
        if self.forecast_error_scale <= 0:
            raise ContractError("Forecast error scale must be positive")
        if self.property not in {"mean", "variance", "quantile"}:
            raise ContractError("Unsupported estimate property")
        if self.property == "quantile" and (self.quantile is None or not 0 <= self.quantile <= 1):
            raise ContractError("Quantile level must be in [0, 1]")


@dataclass(frozen=True)
class ReturnEvent:
    """Scalar event lower < return <= upper; None denotes an infinite bound."""

    # Boundary convention matters when scoring probabilities: equality belongs
    # to the upper bound, not the lower bound, including discrete realized returns.
    lower: float | None = None
    upper: float | None = None

    def __post_init__(self):
        finite(*(x for x in (self.lower, self.upper) if x is not None))
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ContractError("Event bounds must be increasing")


@dataclass(frozen=True)
class Probability:
    """Probabilities for lower-open, upper-closed scalar return events.

    Attributes:
        events: Event boundaries in payload order.
        probabilities: One probability in [0, 1] per event.
        exhaustive: Require a real-line partition with probabilities summing to one.

    Nonexhaustive events are separate claims and may overlap.
    """
    events: tuple[ReturnEvent, ...]
    probabilities: tuple[float, ...]
    exhaustive: bool = False

    def __post_init__(self):
        # A list of overlapping event claims need not sum to one. Only an exhaustive
        # partition must cover the real line without gaps/overlaps and sum to one.
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "probabilities", tuple(self.probabilities))
        if not self.events or len(self.events) != len(self.probabilities):
            raise ContractError("Each event needs a probability")
        finite(*self.probabilities)
        if any(p < 0 or p > 1 for p in self.probabilities):
            raise ContractError("Probabilities must be in [0, 1]")
        if self.exhaustive:
            if abs(sum(self.probabilities) - 1) > 1e-10:
                raise ContractError("Exhaustive probabilities must sum to one")
            bounds = [
                (
                    float("-inf") if e.lower is None else e.lower,
                    float("inf") if e.upper is None else e.upper,
                )
                for e in self.events
            ]
            bounds.sort()
            if (
                bounds[0][0] != float("-inf")
                or bounds[-1][1] != float("inf")
                or any(a[1] != b[0] for a, b in zip(bounds, bounds[1:]))
            ):
                raise ContractError("Exhaustive events must partition the real line")


class PredictiveDistribution(Protocol):
    """Continuous densities use pdf; discrete distributions expose masses separately."""

    support: tuple[float, float]
    units: str

    def pdf(self, value: float) -> float:
        """Return the density at a value in the law's declared units."""
        ...


@dataclass(frozen=True)
class Distribution:
    """A predictive law with explicit support and units and immutable behavior."""
    # The law supplies predictive behavior (density or event masses) and units/support.
    # Wrapping a law does not establish that every downstream model can consume it.
    law: PredictiveDistribution

    def __post_init__(self):
        if not getattr(self.law, "units", None) or not hasattr(self.law, "support"):
            raise ContractError("Distribution needs explicit support and units")


@dataclass(frozen=True)
class Ordering:
    """Highest-to-lowest expected-return groups; instruments within a group are tied."""
    # Groups express ties and order, not cardinal expected-return differences.
    # Consumers interpret temperature explicitly; rankings do not imply calibrated means.
    groups: tuple[tuple[InstrumentId, ...], ...]
    temperature: float
    property: str = "mean"

    def __post_init__(self):
        object.__setattr__(self, "groups", tuple(tuple(g) for g in self.groups))
        ids = [i for g in self.groups for i in g]
        finite(self.temperature)
        if not self.groups or any(not g for g in self.groups) or len(set(ids)) != len(ids):
            raise ContractError("Rankings require unique instruments in nonempty tied groups")
        if self.temperature <= 0 or self.property != "mean":
            raise ContractError("Expected-return ranking needs positive temperature")


@dataclass(frozen=True)
class Score:
    """An uncalibrated scalar with an identity specifying its calibration meaning."""
    # Calibration identity tells a consumer how this raw score should be understood.
    # It must not silently treat a ranking/momentum statistic as a return probability.
    value: float
    calibration_id: str

    def __post_init__(self):
        finite(self.value)
        if not self.calibration_id:
            raise ContractError("A score needs a calibration identity")


class View:
    """Marker base for concrete predictive and action messages."""

    metadata: Metadata


def _scalar_return(target: Target) -> None:
    if (
        not isinstance(target.subject, InstrumentId)
        or target.quantity != "return"
        or not isinstance(target.time, Interval)
        or target.units != "decimal"
    ):
        raise ContractError("Expected a scalar decimal interval-return target")


@dataclass(frozen=True)
class ReturnView(View):
    """A mean decimal simple-return forecast for one instrument and exact interval."""
    target: Target
    statement: Estimate
    metadata: Metadata

    def __post_init__(self):
        _scalar_return(self.target)
        if self.statement.property != "mean":
            raise ContractError("ReturnView expresses expected return")

    @classmethod
    def create(
        cls,
        instrument: InstrumentId,
        interval: Interval,
        mean: float,
        error: float,
        metadata: Metadata,
        convention: str = "price_return",
    ):
        """Construct a scalar mean-return forecast.

        Args:
            instrument: Instrument being forecast.
            interval: Exact start and end of the return horizon.
            mean: Expected decimal simple return.
            error: Positive forecast-error scale in return units.
            metadata: Immutable identity, information cutoff, and expiry.
            convention: Measurement convention for the return.
        """
        # Separate the predicted interval from Metadata expiry/information cutoff.
        # The View combines an explicit return target, mean/error statement and provenance.
        return cls(return_target(instrument, interval, convention), Estimate(mean, error), metadata)


@dataclass(frozen=True)
class RelativeReturnView(View):
    """A mean left-minus-right return forecast for distinct aligned instruments."""
    target: Target
    statement: Estimate
    metadata: Metadata

    def __post_init__(self):
        if self.target.quantity != "difference" or len(self.target.components) != 2:
            raise ContractError("RelativeReturnView requires a difference target")
        a, b = self.target.components
        _scalar_return(a)
        _scalar_return(b)
        if (
            a.time != b.time
            or a.convention != b.convention
            or a.subject == b.subject
            or self.statement.property != "mean"
            or self.target.time != a.time
            or self.target.units != "decimal"
            or self.target.convention != a.convention
            or self.target.subject != (a.subject, b.subject)
        ):
            raise ContractError("Relative returns need distinct, aligned mean targets")

    @classmethod
    def create(
        cls,
        left: InstrumentId,
        right: InstrumentId,
        interval: Interval,
        mean: float,
        error: float,
        metadata: Metadata,
    ):
        """Construct a left-minus-right mean price-return forecast.

        Args:
            left: Instrument whose return is added.
            right: Distinct instrument whose return is subtracted.
            interval: Shared return horizon.
            mean: Expected return difference in decimal units.
            error: Positive forecast-error scale for that difference.
            metadata: Immutable identity, information cutoff, and expiry.
        """
        # The order is left minus right; both component returns use the same interval.
        # This is a difference forecast, not two independent position instructions.
        target = Target(
            (left, right),
            "difference",
            interval,
            components=(return_target(left, interval), return_target(right, interval)),
        )
        return cls(target, Estimate(mean, error), metadata)


@dataclass(frozen=True)
class RankingView(View):
    """An expected-return ordering over the exact instruments in a joint target."""
    target: Target
    statement: Ordering
    metadata: Metadata

    def __post_init__(self):
        ids = tuple(i for g in self.statement.groups for i in g)
        if (
            self.target.subject != ids
            or self.target.quantity != "return"
            or not isinstance(self.target.time, Interval)
            or self.target.units != "decimal"
        ):
            raise ContractError("Ranking target must match its ordered instruments")

    @classmethod
    def create(
        cls,
        groups: tuple[tuple[InstrumentId, ...], ...],
        interval: Interval,
        temperature: float,
        metadata: Metadata,
    ):
        """Construct an expected-return ranking with explicit ties.

        Args:
            groups: Highest-to-lowest nonempty groups with unique instrument identities.
            interval: Shared forecast horizon.
            temperature: Positive ranking-strength parameter interpreted by consumers.
            metadata: Immutable identity, information cutoff, and expiry.
        """
        # Flatten tied groups only to establish the exact ordered joint subject.
        # The grouped Ordering retains the ties for downstream interpretation.
        ordering = Ordering(groups, temperature)
        ids = tuple(i for g in ordering.groups for i in g)
        return cls(Target(ids, "return", interval), ordering, metadata)


@dataclass(frozen=True)
class ProbabilityView(View):
    """Event probabilities for one scalar decimal interval-return target."""
    # Predictive event probabilities remain separate from requested holdings.
    # A portfolio node decides what positions, if any, those beliefs justify.
    target: Target
    statement: Probability
    metadata: Metadata

    def __post_init__(self):
        _scalar_return(self.target)


@dataclass(frozen=True)
class DistributionView(View):
    """A scalar or joint predictive law whose units match its explicit target."""
    # A joint law owns its dependence assumptions. The wrapper does not create
    # a default correlation model or assume independence between instruments.
    target: Target
    statement: Distribution
    metadata: Metadata

    def __post_init__(self):
        if self.target.units != self.statement.law.units:
            raise ContractError("Distribution and target units differ")


@dataclass(frozen=True)
class ScoreView(View):
    """A raw predictive score; no return or probability calibration is implied."""
    # This View is deliberately uncalibrated; a consumer must explicitly map its
    # score to another claim before using return/probability-specific machinery.
    target: Target
    statement: Score
    metadata: Metadata


@dataclass(frozen=True)
class PositionView(View):
    """Desired positions within an explicit scope, including any required exits.

    Attributes:
        positions: Signed amounts by stable instrument identity; zero expresses exit.
        units: Quantity in shares/contracts, or weight as a fraction of account NAV.
        scope: Branch or account identity for position composition.
        metadata: Immutable target identity, cutoff, and expiry.
        attribution: Optional branch contributions in the same position units.

    Omission or withdrawal does not liquidate holdings. Producers must emit zero
    for previously managed instruments they intend to exit.
    """

    # This is a desired-position snapshot, not a record of executed trades.
    # An omitted instrument preserves holdings; include zero when the strategy wants exit.
    positions: Mapping[InstrumentId, float]
    units: str
    scope: str
    metadata: Metadata
    attribution: Mapping[str, Mapping[InstrumentId, float]] | None = None

    def __post_init__(self):
        # Freeze both the account target and optional branch attribution. Attribution
        # explains contributions; execution still reconciles the final account positions.
        finite(*self.positions.values())
        if self.units not in {"quantity", "weight"} or not self.scope:
            raise ContractError("Position units must be quantity or weight, with explicit scope")
        object.__setattr__(self, "positions", frozen_map(self.positions))
        object.__setattr__(
            self,
            "attribution",
            frozen_map({k: frozen_map(v) for k, v in (self.attribution or {}).items()}),
        )


PREDICTIVE_TYPES = (
    ScoreView,
    ReturnView,
    RelativeReturnView,
    RankingView,
    ProbabilityView,
    DistributionView,
)
