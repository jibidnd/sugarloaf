"""Immutable issued forecasts, separate from the runtime's replaceable active Views."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from sugarloaf.core.errors import ContractError
from sugarloaf.core.events import EventBatch
from sugarloaf.core.node import NodeOutputs
from sugarloaf.core.views import PREDICTIVE_TYPES, PositionView, View


@dataclass(frozen=True)
class ForecastRecord:
    """Original predictive contents and the first batch that exposed them."""

    # Keep the original immutable claim plus its first observable issuance clocks.
    # Graph cutoff and receipt describe different boundaries and need not coincide.
    view: View
    node_id: str
    port: str
    batch_sequence: int
    graph_time: datetime
    received_at: datetime


class ForecastArchive:
    """Record each distinct predictive identity once, preserving its first issue.

    Wire ``record`` to GraphRuntime.on_output. Empty/replacement emissions do not
    erase earlier forecasts; expiry is independent of outcome maturity. Repeating
    an unchanged identity is a no-op. This in-memory archive is not durable state.
    PositionViews are actions and are deliberately excluded from prediction research.
    """

    def __init__(self) -> None:
        # This ledger is independent of active runtime authority: expiration,
        # replacement or withdrawal does not erase a prediction that was issued.
        self._records: dict[str, ForecastRecord] = {}

    def record(self, node_id: str, outputs: NodeOutputs, batch: EventBatch) -> None:
        """Archive a validated emission atomically, keyed by predictive View identity.

        Args:
            node_id: Registered producer identity.
            outputs: Complete emitted snapshot; PositionViews are ignored.
            batch: Admission batch establishing first graph and receipt times.

        Raises:
            ContractError: A View is unsupported, duplicated, misaligned, or changed
                under an existing identity.

        Unchanged repeats preserve their first issue. Withdrawal never erases history.
        """
        # Validate the whole emission in a temporary map before publishing it.
        # A malformed later port must not leave earlier ports partially archived.
        pending: dict[str, ForecastRecord] = {}
        # EventBatch resolves an omitted receipt timestamp to its input cutoff.
        # Preserve this first issue time across unchanged repeat output.
        received_at = batch.received_at
        for port, views in outputs.by_port.items():
            for view in views:
                # Positions express actions, not outcome predictions. Recording
                # them as forecasts would mix portfolio decisions with signal skill.
                if isinstance(view, PositionView):
                    continue
                if not isinstance(view, PREDICTIVE_TYPES):
                    raise ContractError(f"Unsupported archive View: {type(view).__name__}")
                metadata = view.metadata
                # A claim must belong to its emitting node and cannot assert an
                # information cutoff beyond the batch that made it observable.
                if metadata.source != node_id or metadata.cutoff > batch.cutoff:
                    raise ContractError("Forecast source/cutoff differs from its issuing batch")
                if metadata.view_id in pending:
                    raise ContractError("Repeated predictive identity in one emission")
                # Identity reuse is allowed only for unchanged content/source.
                # A revision needs a new View ID; it cannot rewrite the old claim.
                previous = self._records.get(metadata.view_id)
                if previous is not None:
                    try:
                        unchanged = previous.view == view and previous.node_id == node_id
                    except (TypeError, ValueError) as exc:
                        raise ContractError(
                            "Forecast identity contents cannot be compared"
                        ) from exc
                    if not unchanged:
                        raise ContractError("Forecast identity reused with changed contents")
                    # Keep the first issuance instead of giving a repeated claim
                    # a later receipt time or counting it as another forecast.
                    pending[metadata.view_id] = previous
                else:
                    pending[metadata.view_id] = ForecastRecord(
                        view, node_id, port, batch.sequence, batch.cutoff, received_at
                    )
        # A bad identity cannot leave half an emission in the archive.
        self._records.update(pending)

    def __iter__(self) -> Iterator[ForecastRecord]:
        # Snapshot the records for iteration so later emissions do not alter the
        # collection being traversed by an evaluation already in progress.
        return iter(tuple(self._records.values()))

    def __len__(self) -> int:
        return len(self._records)

    def get(self, view_id: str) -> ForecastRecord:
        """Return the original record for a predictive identity.

        Raises:
            KeyError: The identity has never been archived.
        """
        return self._records[view_id]
