"""Research kernels on QuantConnect's Jupyter server: connect, start, run a cell, stop.

QC's research server is reached through ``research/read`` and ``research/create``,
the endpoints QC's VS Code extension uses. QC support said (19 October 2026) that
they are not part of its official API. Kernel operations then use the server's
standard Jupyter REST and websocket interface with the bearer token QC returns.
Tokens stay in memory.

A :class:`ResearchKernel` is a persistent kernel: variables survive between
cells, and an idle timeout lets its owner stop it so the account's single
research node is not held indefinitely. ``websocket-client`` is needed to run
cells and is imported only then.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .client import QCClient, QCError, Transport, urllib_transport

MISSING_CONNECTION = "Connection not found"
ALLOCATION_TIMEOUT = "Timeout waiting for a node allocation"


@dataclass
class ResearchServer:
    """One project's Jupyter server and its bearer token."""

    project_id: int
    base: str
    token: str = field(repr=False)
    transport: Transport = field(default=urllib_transport, repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.base)
        # The token is sent to this address; refuse anything that is not QC's.
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".quantconnect.com"):
            raise RuntimeError("Unexpected research server; refusing to transmit credentials")
        self.base = self.base.rstrip("/")

    def request(self, method: str, route: str, body: Any = None, timeout: float = 60):
        """Return (HTTP status, parsed JSON or None) for one Jupyter REST call."""
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        response = self.transport(method, self.base + route, headers, data, timeout)
        try:
            parsed = json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return response.status, parsed

    def call(self, method: str, route: str, body: Any = None, timeout: float = 60):
        status, data = self.request(method, route, body, timeout)
        if not 200 <= status < 300:
            raise RuntimeError(f"Jupyter {method} {route.split('?')[0]}: HTTP {status}")
        return data

    def status(self) -> dict | None:
        """Server status (kernel count, last activity), or None if the server is gone.

        ``/api/status`` does not count as activity for Jupyter's idle culling.
        """
        status, data = self.request("GET", "/api/status")
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise RuntimeError(f"Jupyter /api/status: HTTP {status}")
        return data

    def kernels(self) -> list[dict]:
        return self.call("GET", "/api/kernels")

    def socket_url(self, kernel_id: str, session_id: str) -> str:
        parsed = urlsplit(self.base)
        path = parsed.path.rstrip("/") + f"/api/kernels/{kernel_id}/channels"
        return urlunsplit(("wss", parsed.netloc, path, f"session_id={session_id}", ""))


def _server(project_id: int, record: dict, transport: Transport) -> ResearchServer:
    return ResearchServer(project_id, record["uri"], record["token"], transport)


def connect(
    client: QCClient,
    project_id: int,
    *,
    create: bool = True,
    transport: Transport = urllib_transport,
) -> ResearchServer:
    """Return the project's research server, creating a connection only when needed.

    A connection is created only for the known missing-connection reply, or when
    the saved session's server answers 404 (a session can outlive its server;
    observed 9 October 2026). Other failures, including QC's "Timeout waiting for
    a node allocation", are raised for the caller to handle.
    """
    try:
        record = client.post("research/read", {"projectId": project_id})
    except QCError as error:
        if MISSING_CONNECTION not in str(error) or not create:
            raise
        record = client.post("research/create", {"projectId": project_id}, timeout=600)
    server = _server(project_id, record, transport)
    if server.status() is None:
        if not create:
            raise RuntimeError("The saved research session's server no longer exists")
        record = client.post("research/create", {"projectId": project_id}, timeout=600)
        server = _server(project_id, record, transport)
    return server


def start_kernel(server: ResearchServer) -> str:
    """Start a new python3 kernel on the server and return its ID."""
    return server.call("POST", "/api/kernels", {"name": "python3"})["id"]


def kernel_status(server: ResearchServer, kernel_id: str) -> dict | None:
    """Jupyter's record of one kernel (execution_state, last_activity), or None if gone."""
    status, data = server.request("GET", f"/api/kernels/{kernel_id}")
    if status == 404:
        return None
    if not 200 <= status < 300:
        raise RuntimeError(f"Jupyter kernel status: HTTP {status}")
    return data


def interrupt_kernel(server: ResearchServer, kernel_id: str) -> None:
    server.call("POST", f"/api/kernels/{kernel_id}/interrupt")


def stop_kernel(server: ResearchServer, kernel_id: str) -> bool:
    """Delete one kernel; True if it existed. Other kernels and the server are untouched."""
    status, _ = server.request("DELETE", f"/api/kernels/{kernel_id}")
    if status == 404:
        return False
    if not 200 <= status < 300:
        raise RuntimeError(f"Jupyter kernel delete: HTTP {status}")
    return True


@dataclass
class CellResult:
    """Outcome of one cell: ``ok``, ``error`` or ``timeout``, with nbformat-style outputs."""

    status: str
    execution_count: int | None
    outputs: list[dict]
    elapsed: float

    def text(self) -> str:
        """Stream text, results and errors as plain text."""
        parts = []
        for output in self.outputs:
            kind = output["output_type"]
            if kind == "stream":
                parts.append(output["text"])
            elif kind == "error":
                parts.append(f"{output['ename']}: {output['evalue']}\n")
            elif "text/plain" in output.get("data", {}):
                parts.append(str(output["data"]["text/plain"]) + "\n")
            elif output.get("data"):
                parts.append(f"[{', '.join(output['data'])}]\n")
        return "".join(parts)


def execute_request(code: str, session_id: str) -> tuple[str, dict]:
    """A Jupyter execute_request message with its own parent ID."""
    msg_id = uuid.uuid4().hex
    return msg_id, {
        "header": {
            "msg_id": msg_id,
            "username": "sugarloaf",
            "session": session_id,
            "msg_type": "execute_request",
            "version": "5.3",
            "date": datetime.now(UTC).isoformat(),
        },
        "parent_header": {},
        "metadata": {},
        "channel": "shell",
        "buffers": [],
        "content": {
            "code": code,
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
    }


def _output(kind: str, content: dict) -> dict:
    if kind == "stream":
        return {"output_type": "stream", "name": content["name"], "text": content["text"]}
    if kind == "error":
        return {
            "output_type": "error",
            "ename": content["ename"],
            "evalue": content["evalue"],
            "traceback": content["traceback"],
        }
    output = {
        "output_type": kind,
        "data": content["data"],
        "metadata": content.get("metadata", {}),
    }
    if kind == "execute_result":
        output["execution_count"] = content["execution_count"]
    return output


def collect(
    socket,
    msg_id: str,
    timeout: float,
    on_output: Callable[[dict], None] | None = None,
) -> CellResult:
    """Read one cell's messages until both the shell reply and the idle status arrive.

    Channels multiplex messages; only those whose parent is ``msg_id`` belong to
    this cell. Either marker alone can arrive before all outputs. Raises
    TimeoutError when ``timeout`` seconds pass first.
    """
    started = time.monotonic()
    deadline = started + timeout
    idle = replied = failed = False
    count, outputs = None, []
    while not (idle and replied):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Cell exceeded {timeout:g}s")
        socket.settimeout(remaining)
        try:
            raw = socket.recv()
        except Exception as error:
            if type(error).__name__ in {"WebSocketTimeoutException", "TimeoutError", "timeout"}:
                raise TimeoutError(f"Cell exceeded {timeout:g}s") from error
            raise
        if isinstance(raw, bytes):
            raise RuntimeError("Unsupported binary kernel message")
        message = json.loads(raw)
        if message.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        kind = message["header"]["msg_type"]
        content = message["content"]
        if kind == "status":
            idle = content.get("execution_state") == "idle"
        elif kind == "execute_reply":
            replied = True
            count = content.get("execution_count")
            failed |= content.get("status") == "error"
        elif kind in {"stream", "display_data", "execute_result", "error"}:
            failed |= kind == "error"
            output = _output(kind, content)
            outputs.append(output)
            if on_output:
                on_output(output)
        elif kind == "clear_output":
            outputs = []
    return CellResult("error" if failed else "ok", count, outputs, time.monotonic() - started)


def open_socket(server: ResearchServer, kernel_id: str, timeout: float):
    import websocket

    return websocket.create_connection(
        server.socket_url(kernel_id, uuid.uuid4().hex),
        header={"Authorization": "Bearer " + server.token},
        timeout=timeout,
        suppress_origin=True,
    )


def run_cell(
    server: ResearchServer,
    kernel_id: str,
    code: str,
    *,
    timeout: float = 600,
    on_output: Callable[[dict], None] | None = None,
    socket_factory: Callable[[ResearchServer, str, float], Any] = open_socket,
    interrupt_on_timeout: bool = True,
) -> CellResult:
    """Run one cell on an existing kernel and return its outputs.

    On timeout the kernel is interrupted (not deleted), so its variables survive,
    and the result has status ``timeout`` with the outputs received so far.
    """
    socket = socket_factory(server, kernel_id, timeout)
    received: list[dict] = []

    def keep(output):
        received.append(output)
        if on_output:
            on_output(output)

    started = time.monotonic()
    try:
        msg_id, message = execute_request(code, uuid.uuid4().hex)
        socket.send(json.dumps(message))
        try:
            return collect(socket, msg_id, timeout, keep)
        except TimeoutError:
            if interrupt_on_timeout:
                interrupt_kernel(server, kernel_id)
            return CellResult("timeout", None, received, time.monotonic() - started)
    finally:
        socket.close()


@dataclass
class ResearchKernel:
    """A persistent kernel with an idle timeout.

    Use :meth:`start` for a new kernel or :meth:`attach` to resume one whose ID
    was saved (for example after the owning process restarted). ``idle_timeout``
    is a local policy: :meth:`expired` says when the owner should call
    :meth:`stop`; QC's own culling may end a kernel sooner or later.
    """

    server: ResearchServer
    kernel_id: str
    idle_timeout: float = 1800
    started_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_used: float = field(default_factory=time.monotonic)
    cells: int = 0

    @classmethod
    def start(
        cls, client: QCClient, project_id: int, *, idle_timeout: float = 1800, **connect_kwargs
    ) -> ResearchKernel:
        server = connect(client, project_id, **connect_kwargs)
        return cls(server, start_kernel(server), idle_timeout)

    @classmethod
    def attach(
        cls, client: QCClient, project_id: int, kernel_id: str, *, idle_timeout: float = 1800,
        started_utc: str | None = None, **connect_kwargs
    ) -> ResearchKernel:
        server = connect(client, project_id, create=False, **connect_kwargs)
        if kernel_status(server, kernel_id) is None:
            raise RuntimeError(f"Kernel {kernel_id} no longer exists")
        kernel = cls(server, kernel_id, idle_timeout)
        if started_utc:
            kernel.started_utc = started_utc
        return kernel

    def run(self, code: str, *, timeout: float = 600, **kwargs) -> CellResult:
        self.last_used = time.monotonic()
        try:
            result = run_cell(self.server, self.kernel_id, code, timeout=timeout, **kwargs)
        finally:
            self.last_used = time.monotonic()
        self.cells += 1
        return result

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def expired(self) -> bool:
        return self.idle_seconds() > self.idle_timeout

    def status(self) -> dict:
        remote = kernel_status(self.server, self.kernel_id)
        return {
            "project_id": self.server.project_id,
            "kernel_id": self.kernel_id,
            "alive": remote is not None,
            "execution_state": remote and remote.get("execution_state"),
            "last_activity": remote and remote.get("last_activity"),
            "connections": remote and remote.get("connections"),
            "started_utc": self.started_utc,
            "cells": self.cells,
            "idle_seconds": round(self.idle_seconds(), 1),
            "idle_timeout": self.idle_timeout,
        }

    def interrupt(self) -> None:
        interrupt_kernel(self.server, self.kernel_id)

    def stop(self) -> bool:
        return stop_kernel(self.server, self.kernel_id)
