"""Failures distinguish invalid contracts, unavailable data, and numerical failure."""


class SugarloafError(Exception):
    # Structured codes let diagnostics distinguish causes without parsing prose.
    code = "SUGARLOAF_ERROR"


class ContractError(SugarloafError, ValueError):
    # Invalid identities, units or wiring are programmer/contract failures; the
    # runtime propagates them instead of silently keeping a previous result.
    code = "CONTRACT_INVALID"


class CapabilityError(SugarloafError):
    # The requested provider/service feature is unsupported, not merely warming up.
    code = "CAPABILITY_UNSUPPORTED"


class NotReady(SugarloafError):
    # Expected missing/stale evidence withdraws output and records a diagnostic.
    # A later admitted batch may make the same declared calculation ready.
    code = "DATA_NOT_READY"


class SnapshotError(SugarloafError):
    # A Context read after release or provider mutation is unsafe even if the
    # underlying Python service object is still reachable.
    code = "SNAPSHOT_INVALID"


class NumericalError(SugarloafError):
    # A declared calculation failed numerically. Runtime withdraws the node output
    # and records this cause rather than inventing a usable forecast.
    code = "NUMERICAL_FAILURE"
