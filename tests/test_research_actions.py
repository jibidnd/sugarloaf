"""Causal lifecycle admission and outcome conventions across corporate actions."""

from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from test_adapter_quantconnect import (
    NOW,
    A,
    IndicatorAPI,
    Signal,
    algorithm,
    compiled,
    native_bar,
)

from sugarloaf.adapters.quantconnect import QuantBookServices
from sugarloaf.core.events import LifecycleEvent
from sugarloaf.market.contracts import (
    BarsRequirement,
    IndicatorRequirement,
    ResourceUpdate,
    Selection,
)
from sugarloaf.runtime import GraphRuntime

# These replays read raw prices, the one convention the QuantConnect adapter
# rebuilds on a split. Adjusted series are continuous through a split in LEAN,
# so the adapter leaves those windows and indicators alone.
MINUTE = BarsRequirement(Selection((A,)), timedelta(minutes=1), normalization="raw")


class ActionsAPI(IndicatorAPI):
    def __init__(self, actions):
        super().__init__()
        self.actions = actions

    def corporate_actions(self, algorithm, symbol, start, end):
        return self.actions

    def history(self, algorithm, symbol, req, cutoff, count, period=None):
        self.price = 50 if cutoff >= NOW + timedelta(minutes=2) else 100
        return super().history(algorithm, symbol, req, cutoff, count)

    def history_range(self, algorithm, symbol, source, start, end):
        for index in range(1, 4):
            at = NOW + timedelta(minutes=index)
            if start < at <= end:
                yield native_bar(at, 100 if index < 2 else 50)


def replay(actions):
    api = ActionsAPI(actions)
    provider = QuantBookServices(algorithm(), {A: "A"}, api=api)
    signal = Signal(MINUTE)
    runtime = GraphRuntime(compiled(signal), provider)
    end = NOW + timedelta(minutes=3)
    provider.prepare(NOW, end)
    return api, provider, signal, runtime, end


def test_split_is_admitted_at_its_boundary_before_post_split_prices():
    at = NOW + timedelta(minutes=2)
    api, provider, signal, runtime, end = replay([NS(end_time=at, split_factor=0.5)])
    initial_window = next(iter(provider.windows.values()))
    batches = provider.history_batches(NOW, end)
    first = next(batches)
    runtime.process(first)
    assert not any(isinstance(event.payload, LifecycleEvent) for event in first.events)
    assert next(iter(provider.windows.values())) is initial_window
    action_batch = next(batches)
    # Retrieving the next batch does not make its action visible to the previous Context.
    assert next(iter(provider.windows.values())) is initial_window
    assert action_batch.cutoff == at
    assert action_batch.events[0].payload == LifecycleEvent("split", A, at, factor=0.5)
    runtime.process(action_batch)
    assert next(iter(provider.windows.values())) is not initial_window
    assert signal.reads[-1] == (50.0,)
    assert api.history_reads == 2
    for batch in batches:
        runtime.process(batch)
    assert len(signal.reads) == 3


def test_same_time_actions_are_deduplicated_and_warning_is_not_an_action():
    at = NOW + timedelta(minutes=2)
    split = NS(end_time=at, split_factor=0.5)
    _, provider, _, runtime, end = replay(
        [split, split, NS(end_time=at, distribution=1.0),
         NS(end_time=at, split_factor=0.5, type="Warning")]
    )
    actions = []
    for batch in provider.history_batches(NOW, end):
        actions.extend(event.payload for event in batch.events if event.kind == "lifecycle")
        runtime.process(batch)
    assert {action.kind for action in actions} == {"split", "dividend"}
    assert len(actions) == 2


def test_action_between_bars_has_its_own_batch_without_fabricated_data():
    at = NOW + timedelta(minutes=1, seconds=30)
    _, provider, _, runtime, end = replay([NS(end_time=at, distribution=1.0)])
    seen = []
    for batch in provider.history_batches(NOW, end):
        if batch.cutoff == at:
            seen.append(batch)
            assert [event.kind for event in batch.events] == ["lifecycle"]
        runtime.process(batch)
    assert len(seen) == 1


def test_split_rebuilds_native_indicator_without_replaying_old_price_basis():
    at = NOW + timedelta(minutes=2)
    api = ActionsAPI([NS(end_time=at, split_factor=0.5)])
    provider = QuantBookServices(algorithm(), {A: "A"}, api=api)
    indicator = IndicatorRequirement(MINUTE, "ema", (("period", 2),), window=2)
    provider.install("probe", ResourceUpdate((indicator,), ()))
    end = NOW + timedelta(minutes=3)
    provider.prepare(NOW, end)
    old = next(iter(provider.native_indicators.values()))["native"]
    for batch in provider.history_batches(NOW, end):
        provider.apply(batch)
        snapshot = provider.context(node_id="probe").market.indicator(indicator, A)
        assert all(point.time <= batch.cutoff for point in snapshot.points)
        if batch.cutoff >= at:
            current = next(iter(provider.native_indicators.values()))["native"]
            assert current is not old
            assert all(point.values["value"] == 50 for point in snapshot.points)


@pytest.mark.parametrize("convention,normalization", [
    ("price_return", "split_adjusted"), ("total_return", "total_return")
])
def test_outcome_anchors_use_split_safe_conventions_without_mutating_inputs(
    convention, normalization
):
    api, provider, _, runtime, end = replay([])
    for batch in provider.history_batches(NOW, end):
        runtime.process(batch)
    before = provider.context(node_id="signal").market.bars(MINUTE, A)
    # Use the general boundary's recorded normalization to verify the provider request.
    api.history_range = IndicatorAPI.history_range.__get__(api)
    result = provider.anchor_observation(A, end, convention, end)
    assert result.time == end and api.ranges[-1][2] == normalization
    assert provider.context(node_id="signal").market.bars(MINUTE, A) == before


def admitted_replay():
    """Replay three minutes of A (closes 101, 102, 103); each bar is a clock tick."""
    api = IndicatorAPI()
    provider = QuantBookServices(algorithm(), {A: "A"}, api=api)
    runtime = GraphRuntime(compiled(Signal(MINUTE)), provider)
    end = NOW + timedelta(minutes=3)
    provider.prepare(NOW, end)
    for batch in provider.history_batches(NOW, end):
        runtime.process(batch)
    return api, provider, end


def test_outcome_anchor_is_read_at_the_first_clock_tick_at_or_after_it():
    api, provider, end = admitted_replay()
    minute = lambda n: NOW + timedelta(minutes=n)  # noqa: E731
    # Between ticks: the price at the next tick, reported for the requested time.
    observation = provider.anchor_observation(A, minute(0.5), "price_return", end)
    assert (observation.time, observation.available_at, observation.value) == (
        minute(0.5), minute(1), 101)
    # The session opens at the first tick: no trade yet, so the first trade after it.
    api.session_bounds = lambda security, at, extended: (minute(1), minute(10))
    observation = provider.anchor_observation(A, minute(0.5), "price_return", end)
    assert (observation.available_at, observation.value) == (minute(2), 102)
    # Not known by an earlier availability boundary.
    assert provider.anchor_observation(A, minute(0.5), "price_return", minute(1.5)) is None
    # A session that closes before any trade: missing.
    api.session_bounds = lambda security, at, extended: (minute(1), minute(1.5))
    assert provider.anchor_observation(A, minute(0.5), "price_return", end) is None
    # The clock never reached this time.
    assert provider.anchor_observation(A, minute(3.5), "price_return", end) is None
