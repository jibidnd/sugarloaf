"""A custom structural replay provider uses memory services without QC or a base class."""

from datetime import UTC, datetime, timedelta

from sugarloaf.adapters.memory import MemoryServices
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.node import NodeOutputs, OutputSpec
from sugarloaf.core.types import InstrumentId, Interval, Metadata
from sugarloaf.core.views import Score, ScoreView, return_target
from sugarloaf.market.contracts import Bar, BarsRequirement, Instrument, Selection, Trigger
from sugarloaf.research import ResearchProvider, run_node

START = datetime(2025, 1, 2, tzinfo=UTC)
INSTRUMENT = InstrumentId("fixture:asset")
SOURCE = BarsRequirement(Selection((INSTRUMENT,)), timedelta(minutes=1))


class MemoryReplay:
    """Supplies only the required structural surface; optional metadata is absent."""

    def __init__(self):
        self.memory = MemoryServices((Instrument(INSTRUMENT, "ASSET"),))
        self.pending = None
        self.calls = []

    @property
    def resources(self):
        return self.memory.resources

    def install(self, node_id, update):
        self.calls.append("install")
        self.memory.install(node_id, update)

    def prepare(self, start, end):
        assert self.resources["signal"].requirements == (SOURCE,)
        self.calls.append("prepare")

    def history_batches(self, start, end):
        for sequence, price in enumerate((100, 125), start=1):
            at = start + sequence * timedelta(minutes=1)
            assert at <= end
            self.pending = Bar(
                INSTRUMENT, at - timedelta(minutes=1), at, at,
                open=price, high=price, low=price, close=price, volume=1,
            )
            self.calls.append("yield")
            yield EventBatch(sequence, at, (Event("data", end=at),))
            assert self.pending is None  # Previous admission completed before advancing.

    def apply(self, batch):
        self.calls.append("apply")
        self.memory.put_bars(SOURCE, (self.pending,))
        self.pending = None
        self.memory.apply(batch)

    def context(self, batch=None, node_id=""):
        return self.memory.context(batch, node_id)

    def ready(self, node_id, ctx):
        self.memory.ready(node_id, ctx)

    def schedule_due(self, schedule, after, through):
        return self.memory.schedule_due(schedule, after, through)


class CloseForecast:
    inputs = {}
    outputs = {"forecast": OutputSpec((ScoreView,))}
    requirements = (SOURCE,)
    triggers = (Trigger("data"),)

    def evaluate(self, inputs, ctx):
        latest = ctx.market.bars(SOURCE, INSTRUMENT)[-1]
        assert latest.end == ctx.now
        horizon = Interval(ctx.now, ctx.now + timedelta(days=1))
        view = ScoreView(
            return_target(INSTRUMENT, horizon), Score(latest.close, "fixture-close"),
            Metadata(f"fixture:{ctx.snapshot_id}", ctx.node_id, ctx.now, horizon.end),
        )
        return NodeOutputs({"forecast": (view,)})


def test_public_provider_protocol_accepts_custom_replay_and_preserves_admission_order():
    provider: ResearchProvider = MemoryReplay()
    run = run_node(CloseForecast, provider, start=START, end=START + timedelta(minutes=2))
    assert provider.calls == ["install", "prepare", "yield", "apply", "yield", "apply"]
    assert [record.view.statement.value for record in run.archive] == [100, 125]
    assert run.provenance["provider_details"] == {}
    assert not hasattr(provider, "provenance")
    assert not hasattr(provider, "additional_events")
    assert not hasattr(run.runtime, "execution")
