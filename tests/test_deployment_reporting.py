"""Adapter reporting preserves individual records through a portable callback."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from sugarloaf.adapters.quantconnect.launcher import finish
from sugarloaf.core.errors import ContractError
from sugarloaf.deployment import Environment, ReportContext


def test_report_receives_graph_and_emits_separate_serialized_records():
    logs, summaries = [], []
    decisions = [{"at": datetime(2025, month, 1, tzinfo=UTC)} for month in (1, 2)]
    runtime = NS(graph=NS(nodes={"trend": NS(decisions=decisions)}),
                 diagnostic_count=0, diagnostics=[])
    host = NS(runtime=runtime, sequence=2, services=NS(memberships={}),
              execution=NS(diagnostic_count=0, diagnostics=[]))
    algorithm = NS(log=logs.append, debug=summaries.append)

    def report(context):
        assert isinstance(context, ReportContext)
        assert context.runtime is runtime
        assert not hasattr(context, "algorithm")
        for decision in context.runtime.graph.nodes["trend"].decisions:
            context.record("TREND_DECISION", decision)
        return {"monthly_decision_count": len(decisions)}

    finish(algorithm, NS(report=report), host)
    assert [json.loads(line.split(" ", 1)[1]) for line in logs] == [
        {"at": "2025-01-01T00:00:00+00:00"}, {"at": "2025-02-01T00:00:00+00:00"},
    ]
    assert all(line.startswith("TREND_DECISION ") for line in logs)
    result = json.loads(summaries[0].split(" ", 1)[1])
    assert result["strategy"] == {"monthly_decision_count": 2}


@pytest.mark.parametrize("field", ["cash", "slippage", "leverage"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_environment_rejects_nonfinite_account_settings(field, value):
    with pytest.raises(ContractError, match="finite"):
        Environment(**{field: value})
