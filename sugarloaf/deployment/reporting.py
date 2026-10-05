"""Small JSON conversion shared by deployment reports; no market-data mirror."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timedelta

from sugarloaf.runtime import GraphRuntime


@dataclass(frozen=True)
class ReportContext:
    """Portable end-of-run access to graph state and labeled diagnostic records.

    ``record(label, value)`` sends one record to the adapter's output sink.
    The adapter owns serialization and transport; emit large collections as
    separate records so individual entries remain inspectable.
    """

    # A strategy can inspect its nodes through runtime.graph without knowing the
    # provider. record(label, value) delegates output formatting/transport to the adapter.
    runtime: GraphRuntime
    record: Callable[[str, object], None]


def serialize(value):
    """Convert nested report values to JSON-compatible structures.

    Dataclasses become field dictionaries, mapping keys become strings, dates
    become ISO strings, and timedeltas become seconds. Other values pass through.
    """
    # Use recursive conversion so nested frozen configs and instrument-keyed mappings
    # can enter JSON reports. This helper does not serialize live runtime/provider objects.
    if is_dataclass(value):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serialize(item) for item in value]
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    # Durations use seconds in exported reports; calendar timestamps use ISO text above.
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value
