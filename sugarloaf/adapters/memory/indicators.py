"""Small indicator formulas for the memory adapter and tests.

They follow LEAN's definitions (an EMA seeded by its first value, Wilder's RSI, a
Donchian channel of the last ``period`` highs and lows) closely enough for tests of
node logic. They are fixtures, not a parity promise: a native run reads the
platform's own indicators.
"""

from collections import deque
from datetime import datetime

from sugarloaf.core.data import Bar
from sugarloaf.core.errors import CapabilityError
from sugarloaf.core.inputs import Indicator


class _State:
    def __init__(self, declaration: Indicator):
        self.declaration = declaration
        self.samples = 0
        self.value: float | None = None
        self.time: datetime | None = None
        self.values: deque = deque(maxlen=declaration.window)
        self.outputs: dict[str, float] = {}

    @property
    def is_ready(self) -> bool:
        return self.samples >= self.declaration.period

    def push(self, time: datetime, value: float) -> None:
        self.value, self.time = value, time
        if self.is_ready:
            self.values.appendleft((time, value))


class SMA(_State):
    def __init__(self, declaration):
        super().__init__(declaration)
        self._window: deque = deque(maxlen=declaration.period)

    def update(self, bar: Bar) -> None:
        self._window.append(getattr(bar, self.declaration.field))
        self.samples += 1
        self.push(bar.end, sum(self._window) / len(self._window))


class EMA(_State):
    def __init__(self, declaration):
        super().__init__(declaration)
        smoothing = dict(declaration.parameters).get("smoothing_factor")
        self._k = 2 / (declaration.period + 1) if smoothing is None else float(smoothing)

    def update(self, bar: Bar) -> None:
        price = getattr(bar, self.declaration.field)
        value = price if self.value is None else self.value + self._k * (price - self.value)
        self.samples += 1
        self.push(bar.end, value)


class RSI(_State):
    def __init__(self, declaration):
        super().__init__(declaration)
        self._last: float | None = None
        self._gain = self._loss = 0.0

    def update(self, bar: Bar) -> None:
        price = getattr(bar, self.declaration.field)
        period = self.declaration.period
        if self._last is not None:
            change = price - self._last
            gain, loss = max(change, 0.0), max(-change, 0.0)
            if self.samples < period:
                self._gain += gain / period
                self._loss += loss / period
            else:
                self._gain = (self._gain * (period - 1) + gain) / period
                self._loss = (self._loss * (period - 1) + loss) / period
            self.samples += 1
        self._last = price
        if self.samples == 0:
            return
        value = 100.0 if self._loss == 0 else 100 - 100 / (1 + self._gain / self._loss)
        self.push(bar.end, value)


class Donchian(_State):
    def __init__(self, declaration):
        super().__init__(declaration)
        self._highs: deque = deque(maxlen=declaration.period)
        self._lows: deque = deque(maxlen=declaration.period)

    def update(self, bar: Bar) -> None:
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self.samples += 1
        upper, lower = max(self._highs), min(self._lows)
        self.outputs = {"upper": upper, "lower": lower}
        self.push(bar.end, (upper + lower) / 2)


def create(declaration: Indicator):
    """A fresh indicator state for one instrument."""
    kinds = {"sma": SMA, "ema": EMA, "rsi": RSI, "donchian": Donchian}
    try:
        return kinds[declaration.kind](declaration)
    except KeyError as error:
        raise CapabilityError(f"Memory adapter has no {declaration.kind} indicator") from error
