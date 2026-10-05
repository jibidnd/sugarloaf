"""Stable QC callbacks; deployment_config selects the manifest's Lab/example factory."""

# Packaging generates deployment_config from the manifest-selected factory.
# Selecting another strategy does not require editing these callbacks.
from AlgorithmImports import QCAlgorithm
from deployment_config import load_deployment

from sugarloaf.adapters.quantconnect.launcher import finish, start


class SugarloafAlgorithm(QCAlgorithm):
    # LEAN calls initialize once. The factory reads native string parameters and
    # returns shared settings plus a factory for fresh graph/node state.
    def initialize(self):
        self.deployment = load_deployment(self.get_parameter)
        # Apply account/backtest settings and connect the graph to native services
        # and the execution engine through one callback-serializing host.
        self.host = start(self, self.deployment)

    # The host establishes observation/receipt clocks and admits completed inputs
    # before running the trading session. Nodes do not consume the native Slice.
    def on_data(self, data):
        self.host.on_data(data)

    # Initialization can create subscriptions before the host has been assigned.
    # Later callbacks bind stable identities and queue the next resource boundary.
    def on_securities_changed(self, changes):
        if hasattr(self, "host"):
            self.host.on_securities_changed(changes)

    # Feedback can arrive inside submission. The host queues it until the current
    # decision finishes, then reconciles using cumulative native ticket fills.
    def on_order_event(self, event):
        if hasattr(self, "host"):
            self.host.on_order_event(event)

    # Reporting inspects completed graph state; account performance remains in
    # the native QC report rather than being reconstructed by this entrypoint.
    def on_end_of_algorithm(self):
        finish(self, self.deployment, self.host)
