"""Strategy messages: predictive Views and position requests.

A predictive View has three parts, each set once:

* the target: what is predicted, including its timeframe (``Target.time``);
* the representation: how the belief is expressed (``Estimate``, ``Probability``,
  ``Distribution``, ``Ordering`` or ``Score``);
* metadata: identity, information cutoff and expiry.

``View(target, statement, metadata)`` is the one predictive message type. Which
target kinds a representation may describe is checked when the View is built.
Consumers ask a representation for what they need (``mean()``, ``quantile(p)``,
``probability(lower, upper)``) and declare those needs on their input ports, so a
mean-only consumer accepts an estimate and a full distribution alike.

The named classes (``ReturnView``, ``DistributionView`` and so on) are the earlier
fixed combinations. They remain as subclasses with their stricter checks so
existing nodes keep working; new code builds ``View`` directly.

Returns are decimal simple returns. Weights use account NAV, never branch NAV.
Distributions own any dependence between joint coordinates and must be immutable.
"""

import builtins
import math
from abc import ABC
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata, aware, finite, frozen_map


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


def relative_return(
    left: InstrumentId,
    right: InstrumentId,
    interval: Interval,
    convention: str = "price_return",
) -> Target:
    """Construct the target ``return(left) - return(right)`` over one interval.

    Args:
        left: Instrument whose return is added (for example the stock).
        right: Distinct instrument whose return is subtracted (for example a benchmark).
        interval: Shared horizon of both component returns.
        convention: Measurement rule shared by both components.
    """
    # A relative return is a kind of target, not a kind of View: any representation
    # (a mean, event probabilities, a full distribution) can describe it.
    return Target(
        (left, right),
        "difference",
        interval,
        convention=convention,
        components=(
            return_target(left, interval, convention),
            return_target(right, interval, convention),
        ),
    )


def is_scalar_return(target: Target) -> bool:
    """Whether a target is one instrument's decimal return over one interval."""
    return (
        isinstance(target.subject, InstrumentId)
        and target.quantity == "return"
        and isinstance(target.time, Interval)
        and target.units == "decimal"
    )


def is_relative_return(target: Target) -> bool:
    """Whether a target is a difference of two aligned scalar returns of distinct instruments."""
    if target.quantity != "difference" or len(target.components) != 2:
        return False
    a, b = target.components
    return (
        is_scalar_return(a)
        and is_scalar_return(b)
        and a.time == b.time
        and a.convention == b.convention
        and a.subject != b.subject
        and target.time == a.time
        and target.units == "decimal"
        and target.convention == a.convention
        and target.subject == (a.subject, b.subject)
    )


class Representation:
    """How a View expresses its belief about the target.

    Each representation answers the queries it can (listed in ``capabilities``) and
    raises CapabilityError for the others. A consumer that needs, say, a mean asks
    for ``mean()`` and does not depend on which representation produced it.
    """

    capabilities: frozenset[str] = frozenset()

    def supports(self, query: str) -> bool:
        """Whether this representation can answer ``query`` (``"mean"``, ``"quantile"``, ...)."""
        return query in self.capabilities

    def mean(self) -> float:
        """Expected value of the target."""
        raise CapabilityError(f"{type(self).__name__} does not state a mean")

    def variance(self) -> float:
        """Variance of the target."""
        raise CapabilityError(f"{type(self).__name__} does not state a variance")

    def quantile(self, p: float) -> float:
        """The p-quantile of the target."""
        raise CapabilityError(f"{type(self).__name__} does not state quantiles")

    def probability(self, lower: float | None = None, upper: float | None = None) -> float:
        """P(lower < target <= upper); None is an infinite bound."""
        raise CapabilityError(f"{type(self).__name__} does not state event probabilities")


@dataclass(frozen=True)
class Estimate(Representation):
    """A finite point estimate with a positive forecast-error scale.

    The error scale describes uncertainty in the forecast, not asset volatility.
    The estimate answers only the query for its own property: ``mean()`` for a mean,
    ``variance()`` for a variance, ``quantile(level)`` at its own level.
    """
    # For example, value=0.02 means a 2% simple return when used in a ReturnView.
    # The error scale reports forecast uncertainty; it is not automatically asset sigma.
    value: float
    forecast_error_scale: float
    property: str = "mean"
    level: float | None = None

    def __post_init__(self):
        finite(self.value, self.forecast_error_scale)
        if self.forecast_error_scale <= 0:
            raise ContractError("Forecast error scale must be positive")
        if self.property not in {"mean", "variance", "quantile"}:
            raise ContractError("Unsupported estimate property")
        if self.property == "quantile" and (self.level is None or not 0 <= self.level <= 1):
            raise ContractError("Quantile level must be in [0, 1]")

    # The field named ``property`` shadows the builtin inside this class body.
    @builtins.property
    def capabilities(self) -> frozenset[str]:
        return frozenset({self.property})

    def mean(self) -> float:
        if self.property != "mean":
            return super().mean()
        return self.value

    def variance(self) -> float:
        if self.property != "variance":
            return super().variance()
        return self.value

    def quantile(self, p: float) -> float:
        # One stated quantile answers only for its own level; no interpolation.
        if self.property != "quantile" or p != self.level:
            return super().quantile(p)
        return self.value


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
class Probability(Representation):
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

    capabilities = frozenset({"probability"})

    def probability(self, lower: float | None = None, upper: float | None = None) -> float:
        # Only the events actually stated can be answered; no interpolation between them.
        wanted = ReturnEvent(lower, upper)
        for event, probability in zip(self.events, self.probabilities):
            if event == wanted:
                return probability
        raise CapabilityError("The event was not stated by this Probability")


_INF = math.inf


class Distribution(Representation, ABC):
    """A full predictive distribution over the target.

    Subclasses declare ``units``, ``support`` (low, high) and ``dimension`` (1 for a
    scalar target, n for a joint target of n coordinates), and implement what they
    can: ``cdf`` and ``quantile`` for scalar laws, ``pdf`` or ``logpdf`` for densities.
    The base class derives event probabilities from ``cdf`` and ``pdf``/``logpdf``
    from each other. Moments are not derived, because a quantile function does not
    show whether they exist; a subclass states ``mean`` and ``variance`` itself.

    A distribution of discrete masses may instead expose ``events`` and ``masses``,
    which evaluation scores as categorical probabilities.
    """

    units: str = "decimal"
    support: tuple[float, float] = (-_INF, _INF)
    dimension: int = 1

    _RESERVED = frozenset(
        {"mean", "variance", "quantile", "cdf", "pdf", "logpdf", "probability", "capabilities"}
    )

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # A dataclass field named like a query would take the inherited method as its
        # default and hide the query; such a subclass is rejected when it is defined.
        clash = cls._RESERVED & set(cls.__dict__.get("__annotations__", {}))
        if clash:
            raise TypeError(
                f"{cls.__name__} uses reserved query names as fields: {sorted(clash)}"
            )

    def check(self) -> None:
        """Validate the declared units, support and dimension; called when a View is built."""
        if not isinstance(self.units, str) or not self.units:
            raise ContractError("Distribution needs explicit units")
        if not isinstance(self.dimension, int) or self.dimension < 1:
            raise ContractError("Distribution dimension must be a positive integer")
        # A scalar law has one (low, high); a joint law may give one pair per coordinate.
        support = self.support
        pairs = support if support and isinstance(support[0], tuple) else (support,)
        if len(pairs) not in (1, self.dimension):
            raise ContractError("Joint support needs one (low, high) pair per coordinate")
        for pair in pairs:
            try:
                low, high = pair
            except (TypeError, ValueError):
                raise ContractError("Distribution support must be (low, high)") from None
            if not low < high:
                raise ContractError("Distribution support must have low < high")

    def _implements(self, name: str) -> bool:
        # A query is native when a subclass overrides the base implementation.
        return getattr(type(self), name, None) is not getattr(Distribution, name, None)

    @property
    def capabilities(self) -> frozenset[str]:
        native = {
            name
            for name in ("pdf", "logpdf", "cdf", "quantile", "mean", "variance", "probability")
            if self._implements(name)
        }
        if native & {"pdf", "logpdf"}:
            native |= {"pdf", "logpdf"}
        if "cdf" in native:
            native.add("probability")
        if hasattr(self, "events") and hasattr(self, "masses"):
            native.add("masses")
        return frozenset(native)

    def pdf(self, value) -> float:
        """Density at a value (a coordinate tuple for a joint law)."""
        if not self._implements("logpdf"):
            raise CapabilityError(f"{type(self).__name__} does not state a density")
        return math.exp(self.logpdf(value))

    def logpdf(self, value) -> float:
        """Log density at a value; minus infinity where the density is zero."""
        if not self._implements("pdf"):
            raise CapabilityError(f"{type(self).__name__} does not state a density")
        density = self.pdf(value)
        if not math.isfinite(density) or density < 0:
            raise ContractError(f"{type(self).__name__} returned an invalid density")
        return math.log(density) if density > 0 else -_INF

    def cdf(self, value: float) -> float:
        """P(target <= value) for a scalar law."""
        raise CapabilityError(f"{type(self).__name__} does not state a CDF")

    def probability(self, lower: float | None = None, upper: float | None = None) -> float:
        if not self._implements("cdf"):
            return super().probability(lower, upper)
        ReturnEvent(lower, upper)  # rejects inverted or non-finite bounds
        high = 1.0 if upper is None else self.cdf(upper)
        low = 0.0 if lower is None else self.cdf(lower)
        return max(0.0, high - low)


def _continuous_low_scale(levels, values, lower_bound) -> float:
    """Lower tail scale b at which the tail density meets the first segment's at q_1."""
    # Untruncated, the tail density at q_1 is t_1 / b. Truncated at depth D = q_1 - L it
    # is t_1 / (b (1 - exp(-D / b))); b (1 - exp(-D / b)) rises from 0 towards D, so
    # b (1 - exp(-D / b)) = t_1 / s has one root when t_1 / s < D.
    target = levels[0] * (values[1] - values[0]) / (levels[1] - levels[0])
    if lower_bound is None:
        return target
    depth = values[0] - lower_bound
    if target >= depth:
        return target

    def held(b):
        return -b * math.expm1(-depth / b)

    low, high = target, target
    while held(high) < target:
        high *= 2
    for _ in range(200):
        mid = (low + high) / 2
        if held(mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


@dataclass(frozen=True)
class QuantileDistribution(Distribution):
    """A scalar distribution defined by quantiles, linear between them, exponential beyond.

    Between consecutive quantiles the distribution is uniform, so the quantile
    function is linear: Q(p) = q_j + (p - t_j) / (t_{j+1} - t_j) * (q_{j+1} - q_j).
    Above the last quantile the tail is exponential with scale ``tail_scale_high``:
    Q(p) = q_m - b_high * log((1 - p) / (1 - t_m)). Below the first it is the mirror
    image with scale ``tail_scale_low``, truncated at ``lower_bound`` when one is
    given (-1.0 for a return that cannot fall below -100%). A tail scale left as
    None makes the density continuous at the outer quantile. Below q_1 with a lower
    bound this is possible only when a uniform density equal to the first segment's
    would hold less than t_1 between the bound and q_1; otherwise the untruncated
    scale is used.

    Attributes:
        levels: Probability levels t_1 < ... < t_m, strictly inside (0, 1).
        values: Quantiles q_1 < ... < q_m at those levels, strictly increasing.
        units: Units of the target, "decimal" for returns.
        lower_bound: Optional lowest possible value, below q_1.
        tail_scale_low: Exponential scale below q_1.
        tail_scale_high: Exponential scale above q_m.
    """

    levels: tuple[float, ...]
    values: tuple[float, ...]
    units: str = "decimal"
    lower_bound: float | None = None
    tail_scale_low: float | None = None
    tail_scale_high: float | None = None

    def __post_init__(self):
        levels = tuple(float(x) for x in self.levels)
        values = tuple(float(x) for x in self.values)
        object.__setattr__(self, "levels", levels)
        object.__setattr__(self, "values", values)
        if len(levels) < 2 or len(levels) != len(values):
            raise ContractError(
                "A quantile distribution needs at least two levels with one value each"
            )
        finite(*levels, *values)
        if any(not 0 < t < 1 for t in levels) or any(b <= a for a, b in zip(levels, levels[1:])):
            raise ContractError("Quantile levels must be strictly increasing inside (0, 1)")
        # Crossing or tied quantiles are rejected, not silently repaired: the producer
        # decides how to order its forecasts and records that it did.
        if any(b <= a for a, b in zip(values, values[1:])):
            raise ContractError("Quantile values must be strictly increasing")
        if self.lower_bound is not None:
            finite(self.lower_bound)
            if self.lower_bound >= values[0]:
                raise ContractError("The lower bound must lie below the first quantile")
        # Continuity of the density at the outer quantiles: the tail density there,
        # mass / scale, equals the adjacent segment's density, mass / width.
        low = self.tail_scale_low
        if low is None:
            low = _continuous_low_scale(levels, values, self.lower_bound)
        high = self.tail_scale_high
        if high is None:
            high = (1 - levels[-1]) * (values[-1] - values[-2]) / (levels[-1] - levels[-2])
        finite(low, high)
        if low <= 0 or high <= 0:
            raise ContractError("Tail scales must be positive")
        object.__setattr__(self, "tail_scale_low", float(low))
        object.__setattr__(self, "tail_scale_high", float(high))

    @property
    def support(self) -> tuple[float, float]:
        return (-_INF if self.lower_bound is None else self.lower_bound, _INF)

    def _truncation(self) -> float:
        # exp((L - q_1) / b_low): the share of the untruncated lower tail cut off at L.
        if self.lower_bound is None:
            return 0.0
        return math.exp((self.lower_bound - self.values[0]) / self.tail_scale_low)

    def quantile(self, p: float) -> float:
        if not 0 <= p <= 1:
            raise ContractError("Quantile probability must be in [0, 1]")
        t, q = self.levels, self.values
        if p == 0:
            return self.support[0]
        if p == 1:
            return _INF
        if p < t[0]:
            cut = self._truncation()
            return q[0] + self.tail_scale_low * math.log(cut + (p / t[0]) * (1 - cut))
        if p > t[-1]:
            return q[-1] - self.tail_scale_high * math.log((1 - p) / (1 - t[-1]))
        j = min(bisect_right(t, p) - 1, len(t) - 2)
        return q[j] + (p - t[j]) / (t[j + 1] - t[j]) * (q[j + 1] - q[j])

    def cdf(self, value: float) -> float:
        t, q = self.levels, self.values
        if value <= self.support[0]:
            return 0.0
        if value == _INF:
            return 1.0
        if value < q[0]:
            cut = self._truncation()
            return t[0] * (math.exp((value - q[0]) / self.tail_scale_low) - cut) / (1 - cut)
        if value >= q[-1]:
            return 1 - (1 - t[-1]) * math.exp(-(value - q[-1]) / self.tail_scale_high)
        j = bisect_right(q, value) - 1
        return t[j] + (value - q[j]) / (q[j + 1] - q[j]) * (t[j + 1] - t[j])

    def pdf(self, value: float) -> float:
        t, q = self.levels, self.values
        if value <= self.support[0] or value == _INF:
            return 0.0
        if value < q[0]:
            cut = self._truncation()
            scale = self.tail_scale_low
            return t[0] / (scale * (1 - cut)) * math.exp((value - q[0]) / scale)
        if value >= q[-1]:
            scale = self.tail_scale_high
            return (1 - t[-1]) / scale * math.exp(-(value - q[-1]) / scale)
        j = bisect_right(q, value) - 1
        return (t[j + 1] - t[j]) / (q[j + 1] - q[j])

    def _moments(self) -> tuple[float, float]:
        """E[X] and E[X^2], in closed form, piece by piece."""
        t, q = self.levels, self.values
        low, high = self.tail_scale_low, self.tail_scale_high
        # Lower tail: X = q_1 - Y, Y exponential(b_low) truncated at depth D = q_1 - L.
        cut = self._truncation()
        if cut == 0.0:
            y1, y2 = low, 2 * low**2
        else:
            depth = q[0] - self.lower_bound
            y1 = (low - cut * (depth + low)) / (1 - cut)
            y2 = (2 * low**2 - cut * (depth**2 + 2 * low * depth + 2 * low**2)) / (1 - cut)
        m1 = t[0] * (q[0] - y1)
        m2 = t[0] * (q[0] ** 2 - 2 * q[0] * y1 + y2)
        # Upper tail: X = q_m + Y, Y exponential(b_high).
        m1 += (1 - t[-1]) * (q[-1] + high)
        m2 += (1 - t[-1]) * (q[-1] ** 2 + 2 * q[-1] * high + 2 * high**2)
        # Each interior segment is uniform on [a, b] with probability mass w.
        for j in range(len(t) - 1):
            w, a, b = t[j + 1] - t[j], q[j], q[j + 1]
            m1 += w * (a + b) / 2
            m2 += w * (a * a + a * b + b * b) / 3
        return m1, m2

    def mean(self) -> float:
        return self._moments()[0]

    def variance(self) -> float:
        m1, m2 = self._moments()
        return max(0.0, m2 - m1 * m1)


@dataclass(frozen=True)
class Ordering(Representation):
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
class Score(Representation):
    """An uncalibrated scalar with an identity specifying its calibration meaning."""
    # Calibration identity tells a consumer how this raw score should be understood.
    # It must not silently treat a ranking/momentum statistic as a return probability.
    value: float
    calibration_id: str

    def __post_init__(self):
        finite(self.value)
        if not self.calibration_id:
            raise ContractError("A score needs a calibration identity")


class Message:
    """Base of every graph message: predictive Views and position requests."""

    metadata: Metadata


def _check_pair(target: Target, statement: Representation) -> None:
    """Check that a representation can describe a target."""
    if not isinstance(statement, Representation):
        raise ContractError("A View statement must be a Representation")
    if isinstance(statement, (Estimate, Probability)):
        if not (is_scalar_return(target) or is_relative_return(target)):
            raise ContractError(
                f"{type(statement).__name__} describes a scalar or relative interval return"
            )
    elif isinstance(statement, Distribution):
        statement.check()
        if target.units != statement.units:
            raise ContractError("Distribution and target units differ")
        # A joint target of n instruments has n coordinates; a difference has one.
        joint = isinstance(target.subject, tuple) and target.quantity != "difference"
        if statement.dimension != (len(target.subject) if joint else 1):
            raise ContractError("Distribution dimension differs from the target")
    elif isinstance(statement, Ordering):
        ids = tuple(i for g in statement.groups for i in g)
        if (
            target.subject != ids
            or target.quantity != "return"
            or not isinstance(target.time, Interval)
            or target.units != "decimal"
        ):
            raise ContractError("Ranking target must match its ordered instruments")
    elif not isinstance(statement, Score):
        raise ContractError(f"Unsupported representation: {type(statement).__name__}")


@dataclass(frozen=True)
class View(Message):
    """A predictive claim: a target, a representation of the belief, and metadata.

    Attributes:
        target: What is predicted, with its timeframe.
        statement: The representation (Estimate, Probability, Distribution, Ordering, Score).
        metadata: Identity, information cutoff and expiry.
    """

    target: Target
    statement: Representation
    metadata: Metadata

    def __post_init__(self):
        _check_pair(self.target, self.statement)


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
        # The fixed combination checks run first so their messages stay specific.
        _scalar_return(self.target)
        if not isinstance(self.statement, Estimate) or self.statement.property != "mean":
            raise ContractError("ReturnView expresses expected return")
        super().__post_init__()

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
            not is_relative_return(self.target)
            or not isinstance(self.statement, Estimate)
            or self.statement.property != "mean"
        ):
            raise ContractError("Relative returns need distinct, aligned mean targets")
        super().__post_init__()

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
        return cls(relative_return(left, right, interval), Estimate(mean, error), metadata)


@dataclass(frozen=True)
class RankingView(View):
    """An expected-return ordering over the exact instruments in a joint target."""
    target: Target
    statement: Ordering
    metadata: Metadata

    def __post_init__(self):
        if not isinstance(self.statement, Ordering):
            raise ContractError("RankingView states an Ordering")
        super().__post_init__()

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
        if not isinstance(self.statement, Probability):
            raise ContractError("ProbabilityView states a Probability")
        super().__post_init__()


@dataclass(frozen=True)
class DistributionView(View):
    """A scalar or joint predictive distribution whose units match its explicit target."""
    # A joint distribution owns its dependence assumptions. The View does not create
    # a default correlation model or assume independence between instruments.
    target: Target
    statement: Distribution
    metadata: Metadata

    def __post_init__(self):
        if not isinstance(self.statement, Distribution):
            raise ContractError("DistributionView states a Distribution")
        super().__post_init__()


@dataclass(frozen=True)
class ScoreView(View):
    """A raw predictive score; no return or probability calibration is implied."""
    # This View is deliberately uncalibrated; a consumer must explicitly map its
    # score to another claim before using return/probability-specific machinery.
    target: Target
    statement: Score
    metadata: Metadata

    def __post_init__(self):
        if not isinstance(self.statement, Score):
            raise ContractError("ScoreView states a Score")
        super().__post_init__()


@dataclass(frozen=True)
class PositionView(Message):
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


# Every predictive claim is a View; the named classes are subclasses of it.
PREDICTIVE_TYPES = (View,)
