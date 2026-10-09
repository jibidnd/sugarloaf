"""Issued forecast archives and explicit, outcome-based research evaluation."""

from sugarloaf.research.archive import ForecastArchive, ForecastRecord
from sugarloaf.research.evaluation import (
    EvaluationReport,
    EvaluationRow,
    Outcome,
    OutcomeResolver,
    ReturnOutcomeResolver,
    default_metrics,
    evaluate,
)
from sugarloaf.research.inference import block_bootstrap_interval, cross_sectional_ic
from sugarloaf.research.runner import ResearchProvider, ResearchRun, run_node

__all__ = [
    "EvaluationReport",
    "EvaluationRow",
    "ForecastArchive",
    "ForecastRecord",
    "Outcome",
    "OutcomeResolver",
    "ReturnOutcomeResolver",
    "block_bootstrap_interval",
    "cross_sectional_ic",
    "default_metrics",
    "evaluate",
    "ResearchRun",
    "ResearchProvider",
    "run_node",
]
