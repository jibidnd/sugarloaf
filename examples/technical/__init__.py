"""Portable teaching strategies; see README.md for indicator conventions."""

# Export only these teaching nodes; importing the package does not start a fixture run.
from .nodes import DonchianBreakout, EMACrossover, RSIMeanReversion, build_graph

__all__ = ["DonchianBreakout", "EMACrossover", "RSIMeanReversion", "build_graph"]
