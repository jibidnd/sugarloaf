"""Research kernels: connection choice, cell execution, timeouts and idle expiry.

Jupyter messages follow the sequence recorded from QC's research server; no
network access is used.
"""

import json

import pytest

from sugarloaf.adapters.quantconnect.cloud import kernels
from sugarloaf.adapters.quantconnect.cloud.client import QCError, Response

URI = "https://research.quantconnect.com/node/abc"


class Jupyter:
    """A transport answering Jupyter REST routes from a table of (method, route) -> reply."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        route = url.removeprefix(URI)
        self.calls.append((method, route))
        assert headers["Authorization"] == "Bearer tok"
        status, payload = self.routes[(method, route)]
        return Response(status, json.dumps(payload).encode() if payload is not None else b"")


class QC:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, endpoint, data, **kwargs):
        self.calls.append(endpoint)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


RECORD = {"uri": URI, "token": "tok", "success": True}


def test_connect_reuses_live_session_and_creates_only_when_missing_or_gone():
    alive = Jupyter({("GET", "/api/status"): (200, {"kernels": 0})})
    qc = QC(RECORD)
    server = kernels.connect(qc, 1, transport=alive)
    assert qc.calls == ["research/read"]
    assert "tok" not in repr(server)

    qc = QC(QCError("research/read", "Connection not found."), RECORD)
    kernels.connect(qc, 1, transport=alive)
    assert qc.calls == ["research/read", "research/create"]

    gone = Jupyter({("GET", "/api/status"): (404, None)})
    qc = QC(RECORD, RECORD)
    kernels.connect(qc, 1, transport=gone)
    assert qc.calls == ["research/read", "research/create"]


def test_connect_surfaces_stuck_allocation_and_refuses_foreign_servers():
    stuck = QCError("research/read", "Timeout waiting for a node allocation, please retry.")
    with pytest.raises(QCError, match="node allocation"):
        kernels.connect(QC(stuck), 1)
    with pytest.raises(RuntimeError, match="refusing"):
        kernels.connect(QC({"uri": "https://evil.example.com/x", "token": "tok"}), 1)
    with pytest.raises(QCError):
        kernels.connect(QC(QCError("research/read", "Connection not found.")), 1, create=False)


def message(parent, kind, content):
    return json.dumps({"parent_header": {"msg_id": parent}, "header": {"msg_type": kind},
                       "content": content})


class Socket:
    """A kernel channel replaying a recorded message sequence for the sent request."""

    def __init__(self, script, stall=False):
        self.script, self.stall, self.sent, self.closed = script, stall, [], False

    def send(self, raw):
        self.sent.append(json.loads(raw))
        msg_id = self.sent[-1]["header"]["msg_id"]
        self.queue = [message(msg_id if parent else "other", kind, content)
                      for parent, kind, content in self.script]

    def settimeout(self, value):
        pass

    def recv(self):
        if not self.queue:
            assert self.stall
            raise TimeoutError
        return self.queue.pop(0)

    def close(self):
        self.closed = True


OK_SCRIPT = [
    (True, "status", {"execution_state": "busy"}),
    (False, "stream", {"name": "stdout", "text": "unrelated\n"}),
    (True, "stream", {"name": "stdout", "text": "42\n"}),
    (True, "execute_reply", {"status": "ok", "execution_count": 3}),
    (True, "execute_result", {"data": {"text/plain": "'x'"}, "metadata": {},
                              "execution_count": 3}),
    (True, "status", {"execution_state": "idle"}),
]


def server(routes=None):
    return kernels.ResearchServer(1, URI, "tok", Jupyter(routes or {}))


def test_run_cell_collects_only_its_outputs_until_reply_and_idle():
    socket = Socket(OK_SCRIPT)
    seen = []
    result = kernels.run_cell(server(), "k1", "print(x)", on_output=seen.append,
                              socket_factory=lambda *_: socket)
    assert result.status == "ok" and result.execution_count == 3
    assert result.text() == "42\n'x'\n"
    assert seen == result.outputs
    assert socket.sent[0]["content"]["code"] == "print(x)"
    assert socket.closed


def test_error_cell_and_timeout_interrupts_without_deleting():
    error = [
        (True, "error", {"ename": "NameError", "evalue": "y", "traceback": ["..."]}),
        (True, "execute_reply", {"status": "error", "execution_count": 4}),
        (True, "status", {"execution_state": "idle"}),
    ]
    result = kernels.run_cell(server(), "k1", "y", socket_factory=lambda *_: Socket(error))
    assert result.status == "error" and "NameError: y" in result.text()

    routes = {("POST", "/api/kernels/k1/interrupt"): (204, None)}
    srv = server(routes)
    partial = [(True, "stream", {"name": "stdout", "text": "step 1\n"})]
    result = kernels.run_cell(srv, "k1", "loop()", timeout=0.05,
                              socket_factory=lambda *_: Socket(partial, stall=True))
    assert result.status == "timeout" and result.text() == "step 1\n"
    assert srv.transport.calls == [("POST", "/api/kernels/k1/interrupt")]


def test_persistent_kernel_status_stop_and_idle_expiry(monkeypatch):
    routes = {
        ("GET", "/api/status"): (200, {"kernels": 1}),
        ("POST", "/api/kernels"): (201, {"id": "k1"}),
        ("GET", "/api/kernels/k1"): (200, {"execution_state": "idle", "connections": 0,
                                         "last_activity": "2026-11-08T04:00:00Z"}),
        ("DELETE", "/api/kernels/k1"): (204, None),
    }
    jupyter = Jupyter(routes)
    kernel = kernels.ResearchKernel.start(QC(RECORD), 1, idle_timeout=60, transport=jupyter)
    assert kernel.kernel_id == "k1"
    kernel.run("x = 1", socket_factory=lambda *_: Socket(OK_SCRIPT))
    status = kernel.status()
    assert status["alive"] and status["execution_state"] == "idle" and status["cells"] == 1
    assert not kernel.expired()
    kernel.last_used -= 61
    assert kernel.expired()
    assert kernel.stop()

    attached = kernels.ResearchKernel.attach(QC(RECORD), 1, "k1", transport=jupyter)
    assert attached.kernel_id == "k1"
    routes[("GET", "/api/kernels/k1")] = (404, None)
    with pytest.raises(RuntimeError, match="no longer exists"):
        kernels.ResearchKernel.attach(QC(RECORD), 1, "k1", transport=jupyter)
