"""Stable QC callbacks; deployment_config selects the manifest's Lab/example factory."""

# Packaging generates deployment_config from the manifest-selected factory.
# Selecting another strategy does not require editing these callbacks.
from AlgorithmImports import QCAlgorithm
from deployment_config import load_deployment

from sugarloaf.adapters.quantconnect.launcher import finish, load, start


class SugarloafAlgorithm(QCAlgorithm):
    # LEAN calls initialize once. The factory reads native string parameters (and,
    # if it asks, stored platform files) and returns the deployment: a graph
    # factory plus environment and execution settings.
    def initialize(self):
        self.deployment = load(load_deployment, self)
        # The adapter maps the graph's declarations onto this algorithm and
        # forwards every callback below to the runner.
        self.adapter = start(self, self.deployment)

    def on_data(self, data):
        self.adapter.on_data(data)

    def on_securities_changed(self, changes):
        if hasattr(self, "adapter"):
            self.adapter.on_securities_changed(changes)

    def on_order_event(self, event):
        if hasattr(self, "adapter"):
            self.adapter.on_order_event(event)

    def on_end_of_algorithm(self):
        finish(self, self.deployment, self.adapter)
