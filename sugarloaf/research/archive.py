"""Immutable issued forecasts, separate from the runner's replaceable current views."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from sugarloaf.core.errors import ContractError
from sugarloaf.core.views import PREDICTIVE_TYPES, PositionView, View


@dataclass(frozen=True)
class ForecastRecord:
    """A predictive View and when the runner first saw it issued."""

    view: View
    node_id: str
    port: str
    received_at: datetime


class ForecastArchive:
    """Record each distinct predictive identity once, preserving its first issue.

    Replacement, withdrawal and expiry never erase a forecast that was issued.
    Repeating an unchanged identity is a no-op; changed contents under an existing
    identity are a contract error. Position views are actions, not forecasts, and
    are ignored. Storage is in memory; the runner saves it through the adapter.
    """

    def __init__(self) -> None:
        self._records: dict[str, ForecastRecord] = {}

    def record(self, node_id: str, port: str, views, received_at: datetime) -> None:
        """Archive one port's emission atomically, keyed by predictive View identity.

        Raises:
            ContractError: A View is unsupported, repeated in one emission, from
                another node, or changed under an existing identity.
        """
        pending: dict[str, ForecastRecord] = {}
        for view in views:
            if isinstance(view, PositionView):
                continue
            if not isinstance(view, PREDICTIVE_TYPES):
                raise ContractError(f"Unsupported archive View: {type(view).__name__}")
            metadata = view.metadata
            if metadata.source != node_id or metadata.cutoff > received_at:
                raise ContractError("Forecast source or cutoff differs from its issue")
            if metadata.view_id in pending:
                raise ContractError("Repeated predictive identity in one emission")
            previous = self._records.get(metadata.view_id)
            if previous is not None:
                try:
                    unchanged = previous.view == view and previous.node_id == node_id
                except (TypeError, ValueError) as exc:
                    raise ContractError("Forecast identity contents cannot be compared") from exc
                if not unchanged:
                    raise ContractError("Forecast identity reused with changed contents")
                pending[metadata.view_id] = previous
            else:
                pending[metadata.view_id] = ForecastRecord(view, node_id, port, received_at)
        self._records.update(pending)

    def __iter__(self) -> Iterator[ForecastRecord]:
        return iter(tuple(self._records.values()))

    def __len__(self) -> int:
        return len(self._records)

    def get(self, view_id: str) -> ForecastRecord:
        """The original record for a predictive identity.

        Raises:
            KeyError: The identity has never been archived.
        """
        return self._records[view_id]
