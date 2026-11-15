"""The QuantConnect adapter's bar-order rules for windows and indicators, without LEAN.

``add_in_order`` reads only ``count``, ``[0]``, ``add`` and each bar's ``end_time``, so a
deque-backed window with LEAN's newest-first indexing exercises it. ``update_in_order``
reads ``current.end_time`` and calls ``update``, which a running mean of closes provides.
"""

from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

from sugarloaf.adapters.quantconnect.adapter import (
    PendingFills,
    add_in_order,
    fill_before,
    touch_window,
    update_in_order,
)

DAY = timedelta(days=1)
CLOSE = datetime(2025, 1, 2, 16)  # exchange-local, as LEAN stamps bars


class DequeWindow:
    """A rolling window of fixed size, newest at index 0, as LEAN's ``RollingWindow``."""

    def __init__(self, size):
        self.items = deque(maxlen=size)

    @property
    def count(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def add(self, item):
        self.items.appendleft(item)


def session(k):
    return SimpleNamespace(end_time=CLOSE + k * DAY, close=100.0 + k)


def ends(window):
    return [bar.end_time for bar in window.items]


def test_replayed_sessions_after_a_history_fill_stay_out():
    # History fills sessions 0..9 at subscribe; warm-up replays 5..9; live delivers 10, 11.
    window = DequeWindow(20)
    for k in range(10):
        assert add_in_order(window, session(k))
    assert not any(add_in_order(window, session(k)) for k in range(5, 10))
    assert add_in_order(window, session(10))  # the first bar after warm-up is kept
    assert add_in_order(window, session(11))
    assert ends(window) == [session(k).end_time for k in range(11, -1, -1)]
    assert len(set(ends(window))) == window.count == 12


def test_a_bar_ending_with_the_newest_is_left_out():
    window = DequeWindow(5)
    add_in_order(window, session(0))
    repeat = session(0)  # another object for the same period
    assert not add_in_order(window, repeat)
    assert window.count == 1 and window[0] is not repeat


def test_a_late_bar_is_left_out_and_order_is_kept():
    window = DequeWindow(5)
    for k in (0, 1, 3):
        add_in_order(window, session(k))
    assert not add_in_order(window, session(2))  # arrives after session 3
    assert ends(window) == [session(k).end_time for k in (3, 1, 0)]


def test_an_empty_window_takes_any_bar():
    window = DequeWindow(3)
    assert add_in_order(window, session(-100))
    assert window.count == 1


class RunningMean:
    """The mean of every close read, with LEAN's ``current`` and ``update`` signatures."""

    def __init__(self):
        self.closes, self.bars = [], []
        self.current = SimpleNamespace(end_time=datetime.min, value=0.0)

    def update(self, *arguments):
        if len(arguments) == 1:  # a whole bar, as Donchian reads it
            self.bars.append(arguments[0])
            end, value = arguments[0].end_time, arguments[0].close
        else:
            end, value = arguments
        self.closes.append(value)
        self.current = SimpleNamespace(end_time=end, value=sum(self.closes) / len(self.closes))


def test_an_indicator_reads_each_period_once_through_warm_up_replay():
    mean = RunningMean()
    for k in range(10):  # warm_up_indicator's history
        assert update_in_order(mean, session(k), "close")
    # Replay of sessions 5..9: new bar objects, the last ending with the current value.
    assert not any(update_in_order(mean, session(k), "close") for k in range(5, 10))
    assert update_in_order(mean, session(10), "close")  # the first bar after warm-up
    assert mean.closes == [100.0 + k for k in range(11)]
    assert mean.current.value == 105.0


def test_a_whole_bar_goes_to_an_indicator_without_a_field():
    mean = RunningMean()
    bar = session(0)
    assert update_in_order(mean, bar)
    assert mean.bars == [bar] and not update_in_order(mean, session(0))


def test_a_history_fill_ends_before_the_clock():
    # The clock stands at the close of session 5: a fill takes sessions 0..4, the slice
    # being delivered brings session 5, and sessions 6..7 are later than the clock.
    window = DequeWindow(10)
    history = [session(k) for k in range(8)]
    now = session(5).end_time
    taken = fill_before(history, now, lambda bar: bar.end_time,
                        lambda bar: add_in_order(window, bar))
    assert taken == 5
    assert ends(window) == [session(k).end_time for k in range(4, -1, -1)]
    assert add_in_order(window, session(5))  # the slice's own bar follows the fill
    assert all(end <= now for end in ends(window))


def pending_fill(window, history, performed):
    """A queued window fill: history bars ending before the cutoff go to the window."""
    pending = PendingFills()

    def fill(cutoff, source):
        performed.append(source)
        fill_before(history, cutoff, lambda bar: bar.end_time,
                    lambda bar: add_in_order(window, bar))

    pending.add("daily", fill)
    return pending


def test_a_handler_firing_before_the_fill_leaves_a_complete_window_in_order():
    # LEAN's handler delivers session 10 before any other callback has run the fill, whose
    # history reaches session 10 and beyond (the clock has moved past it).
    window, performed = DequeWindow(8), []
    pending = pending_fill(window, [session(k) for k in range(12)], performed)
    live = session(10)
    assert touch_window(pending, "daily", window, live, live.end_time, "consolidator")
    assert performed == ["consolidator"]
    assert ends(window) == [session(k).end_time for k in range(10, 2, -1)]
    assert window[0] is live
    # Later touches find nothing pending; the next session follows in order.
    assert touch_window(pending, "daily", window, session(11), session(11).end_time, "on_data")
    assert performed == ["consolidator"]
    assert window.count == 8 and len(set(ends(window))) == 8


def test_run_all_performs_every_pending_fill_once():
    window, performed = DequeWindow(5), []
    pending = pending_fill(window, [session(k) for k in range(5)], performed)
    pending.run_all(session(3).end_time, "on_securities_changed")
    pending.run_all(session(9).end_time, "on_data")
    assert performed == ["on_securities_changed"]
    assert ends(window) == [session(k).end_time for k in (2, 1, 0)]
