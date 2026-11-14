"""JSON encoding of a forecast archive, and decoding it back for evaluation.

A forecast run saves its archive where the platform keeps files; a research kernel
reads it back and scores it. The core representations (Estimate, Probability,
Score, Ordering, QuantileDistribution) encode field by field. Any other
representation is pickled and stored as base64 under ``"pickle"``, so decoding it
needs the class importable in the kernel.
"""

import base64
import pickle
from dataclasses import asdict, fields
from datetime import datetime
from typing import Iterable

from sugarloaf.core.errors import ContractError
from sugarloaf.core.primitives import InstrumentId, Interval, Metadata
from sugarloaf.core.views import (
    Estimate,
    Ordering,
    Probability,
    QuantileDistribution,
    ReturnEvent,
    Score,
    Target,
    View,
)
from sugarloaf.research.archive import ForecastArchive, ForecastRecord

_REPRESENTATIONS = {
    cls.__name__: cls for cls in (Estimate, Probability, Score, Ordering, QuantileDistribution)
}


def _encode_subject(subject):
    if isinstance(subject, InstrumentId):
        return {"instrument": subject.value}
    if isinstance(subject, tuple):
        return {"instruments": [s.value for s in subject]}
    return {"name": subject}


def _decode_subject(value):
    if "instrument" in value:
        return InstrumentId(value["instrument"])
    if "instruments" in value:
        return tuple(InstrumentId(v) for v in value["instruments"])
    return value["name"]


def _encode_time(time):
    if isinstance(time, datetime):
        return {"at": time.isoformat()}
    if isinstance(time, Interval):
        return {"start": time.start.isoformat(), "end": time.end.isoformat()}
    return {"intervals": [_encode_time(t) for t in time]}


def _decode_time(value):
    if "at" in value:
        return datetime.fromisoformat(value["at"])
    if "intervals" in value:
        return tuple(_decode_time(t) for t in value["intervals"])
    return Interval(datetime.fromisoformat(value["start"]), datetime.fromisoformat(value["end"]))


def encode_target(target: Target) -> dict:
    return {
        "subject": _encode_subject(target.subject),
        "quantity": target.quantity,
        "time": _encode_time(target.time),
        "units": target.units,
        "convention": target.convention,
        "components": [encode_target(c) for c in target.components],
    }


def decode_target(value: dict) -> Target:
    return Target(
        _decode_subject(value["subject"]),
        value["quantity"],
        _decode_time(value["time"]),
        value["units"],
        value["convention"],
        tuple(decode_target(c) for c in value["components"]),
    )


def encode_statement(statement) -> dict:
    name = type(statement).__name__
    if name in _REPRESENTATIONS and type(statement) is _REPRESENTATIONS[name]:
        data = asdict(statement)
        if isinstance(statement, Probability):
            data["events"] = [asdict(e) for e in statement.events]
        if isinstance(statement, Ordering):
            data["groups"] = [[i.value for i in g] for g in statement.groups]
        return {"type": name, "fields": data}
    return {
        "type": name,
        "pickle": base64.b64encode(pickle.dumps(statement)).decode("ascii"),
    }


def decode_statement(value: dict):
    if "pickle" in value:
        return pickle.loads(base64.b64decode(value["pickle"]))
    cls = _REPRESENTATIONS[value["type"]]
    data = dict(value["fields"])
    if cls is Probability:
        data["events"] = tuple(ReturnEvent(**e) for e in data["events"])
    if cls is Ordering:
        data["groups"] = tuple(tuple(InstrumentId(i) for i in g) for g in data["groups"])
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def encode_view(view: View) -> dict:
    return {
        "class": type(view).__name__,
        "target": encode_target(view.target),
        "statement": encode_statement(view.statement),
        "metadata": {
            **asdict(view.metadata),
            "cutoff": view.metadata.cutoff.isoformat(),
            "expires_at": view.metadata.expires_at.isoformat(),
        },
    }


def decode_view(value: dict) -> View:
    metadata = dict(value["metadata"])
    metadata["cutoff"] = datetime.fromisoformat(metadata["cutoff"])
    metadata["expires_at"] = datetime.fromisoformat(metadata["expires_at"])
    metadata["parents"] = tuple(metadata.get("parents", ()))
    return View(decode_target(value["target"]), decode_statement(value["statement"]),
                Metadata(**metadata))


def encode_archive(archive: Iterable[ForecastRecord]) -> list[dict]:
    return [
        {
            "view": encode_view(record.view),
            "node_id": record.node_id,
            "port": record.port,
            "received_at": record.received_at.isoformat(),
        }
        for record in archive
    ]


def decode_archive(records: Iterable[dict]) -> ForecastArchive:
    archive = ForecastArchive()
    for record in records:
        try:
            view = decode_view(record["view"])
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError(f"Malformed forecast record: {error}") from error
        archive.record(record["node_id"], record["port"], (view,),
                       datetime.fromisoformat(record["received_at"]))
    return archive
