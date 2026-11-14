"""Anchor functions for outcome resolution, built from prices read in a research kernel.

``ReturnOutcomeResolver`` asks an anchor for the price of an instrument at an exact
time. In a kernel the prices come from one ``qb.history`` read; these helpers turn
such a read into an anchor without any look-ahead rule beyond "the price at that
time, known when its bar ended".
"""

from collections.abc import Mapping
from datetime import datetime

from sugarloaf.core.data import Observation
from sugarloaf.core.primitives import InstrumentId


def anchor_from_closes(closes: Mapping[InstrumentId, Mapping[datetime, float]]):
    """An anchor over exact close times: ``{instrument: {bar end (aware): close}}``.

    A time with no close is unavailable, never replaced by a neighbour. The returned
    function has the ``anchor(instrument, at, convention, as_of)`` signature the
    resolver expects; the convention is the caller's responsibility, since one price
    table carries one adjustment convention.
    """

    def anchor(instrument, at, convention, as_of):
        price = closes.get(instrument, {}).get(at)
        if price is None or at > as_of:
            return None
        return Observation(at, float(price))

    return anchor


def closes_from_frame(frame, instrument_of, time_of) -> dict:
    """Build the ``anchor_from_closes`` table from a history DataFrame.

    Args:
        frame: A frame indexed by (symbol, time) with a ``close`` column, as
            ``qb.history`` returns.
        instrument_of: Maps the frame's symbol index value to an ``InstrumentId``.
        time_of: Maps the frame's time index value to an aware bar end.
    """
    table: dict = {}
    for (symbol, time), close in frame["close"].items():
        table.setdefault(instrument_of(symbol), {})[time_of(time)] = float(close)
    return table
