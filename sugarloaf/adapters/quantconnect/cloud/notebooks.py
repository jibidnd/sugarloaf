"""Execute a local notebook on QC's remote Jupyter kernel, preserving outputs.

Uses the research/create and research/read endpoints used by QC's VS Code
extension (not a documented public API stability promise). Requires the existing
authenticated LEAN environment, requests, websocket-client and nbformat.
Connection tokens stay in memory. Only the newly created kernel is shut down.
"""

import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def run_notebook(path: Path, project_id: int, timeout: int = 600):
    # These optional dependencies are loaded only for this remote notebook tool.
    # Ordinary graph execution and offline packaging do not require a Jupyter client.
    import nbformat
    import requests
    import websocket
    from lean.container import container
    from lean.models.errors import RequestFailedError

    api = container.api_client
    try:
        connection = api.post("research/read", {"projectId": project_id})
    except RequestFailedError as error:
        if "Connection not found" not in str(error):
            raise
        # Create a research connection only for the known missing-connection response.
        # Authentication/server failures above must propagate rather than trigger new work.
        connection = api.post("research/create", {"projectId": project_id})
    base = connection["uri"].rstrip("/")
    parsed = urlsplit(base)
    # The returned endpoint will receive a bearer token; validate its origin before
    # opening either REST or websocket connections.
    if parsed.scheme != "https" or not parsed.hostname.endswith(".quantconnect.com"):
        raise RuntimeError("Unexpected research server; refusing to transmit credentials")
    token = connection["token"]
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token

    def request(method, route, **kwargs):
        # Research connection discovery uses LEAN; kernel REST operations use the
        # authenticated Jupyter session with a bounded request timeout.
        result = session.request(method, base + route, timeout=60, **kwargs)
        if not result.ok:
            raise RuntimeError(f"Jupyter {route.split('?')[0]}: HTTP {result.status_code}")
        return result

    notebook = nbformat.read(path, as_version=4)
    # Clear previous code outputs in memory before execution so retained outputs
    # describe this attempt rather than silently mixing new cells with old results.
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs, cell.execution_count = [], None
    # Own a new kernel for this invocation. Cleanup deletes only this kernel ID,
    # not other notebooks or the entire shared research server.
    kernel = request("POST", "/api/kernels", json={"name": "python3"}).json()
    kernel_id = kernel["id"]
    client_id = uuid.uuid4().hex
    channel_path = parsed.path.rstrip("/") + f"/api/kernels/{kernel_id}/channels"
    socket_url = urlunsplit(("wss", parsed.netloc, channel_path, f"session_id={client_id}", ""))
    socket = None
    notebook.metadata["quantconnect_run"] = {
        "project_id": project_id,
        "kernel": "python3",
        "started_utc": datetime.now(UTC).isoformat(),
        "completed": False,
    }
    try:
        socket = websocket.create_connection(
            socket_url,
            header={"Authorization": "Bearer " + token},
            timeout=timeout,
            suppress_origin=True,
        )
        for index, cell in enumerate(notebook.cells):
            if cell.cell_type != "code":
                continue
            # Jupyter channels multiplex messages. Each cell gets its own parent ID so
            # background or unrelated kernel messages cannot be mistaken for its output.
            msg_id = uuid.uuid4().hex
            socket.send(
                json.dumps(
                    {
                        "header": {
                            "msg_id": msg_id,
                            "username": "sugarloaf",
                            "session": client_id,
                            "msg_type": "execute_request",
                            "version": "5.3",
                            "date": datetime.now(UTC).isoformat(),
                        },
                        "parent_header": {},
                        "metadata": {},
                        "channel": "shell",
                        "buffers": [],
                        "content": {
                            "code": cell.source,
                            "silent": False,
                            "store_history": True,
                            "user_expressions": {},
                            "allow_stdin": False,
                            "stop_on_error": True,
                        },
                    }
                )
            )
            print(f"Executing cell {index + 1}/{len(notebook.cells)}", flush=True)
            deadline = time.monotonic() + timeout
            idle, replied, failed = False, False, False
            # Wait for both shell completion and the output-channel idle marker; either
            # one alone can arrive before all cell outputs have been received.
            while not (idle and replied):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Cell {index + 1} exceeded {timeout}s")
                socket.settimeout(remaining)
                raw = socket.recv()
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
                    cell.execution_count = content.get("execution_count")
                    failed |= content.get("status") == "error"
                elif kind in {"stream", "display_data", "execute_result", "error"}:
                    if kind == "stream":
                        output = nbformat.v4.new_output(
                            kind, name=content["name"], text=content["text"]
                        )
                        print(content["text"], end="", flush=True)
                    elif kind == "error":
                        failed = True
                        output = nbformat.v4.new_output(
                            kind,
                            ename=content["ename"],
                            evalue=content["evalue"],
                            traceback=content["traceback"],
                        )
                        print(content["ename"] + ": " + content["evalue"], flush=True)
                    else:
                        fields = {"data": content["data"], "metadata": content.get("metadata", {})}
                        if kind == "execute_result":
                            fields["execution_count"] = content["execution_count"]
                        output = nbformat.v4.new_output(kind, **fields)
                    cell.outputs.append(output)
                elif kind == "clear_output":
                    cell.outputs = []
            # Retain progress cell by cell, including errors, so a failed attempt still
            # has inspectable output rather than losing all evidence at the final cell.
            nbformat.write(notebook, path)
            if failed:
                raise RuntimeError(f"Notebook cell {index + 1} failed; outputs saved")
        notebook.metadata["quantconnect_run"].update(
            completed=True, finished_utc=datetime.now(UTC).isoformat()
        )
        print(f"Completed notebook: {path}", flush=True)
    # Persist partial results and close the owned resources even when a cell fails.
    # The notebook itself is evidence; connection tokens are not written into it.
    finally:
        nbformat.write(notebook, path)
        if socket is not None:
            socket.close()
        request("DELETE", f"/api/kernels/{kernel_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", type=Path)
    parser.add_argument("--project-id", type=int, required=True)
    args = parser.parse_args()
    run_notebook(args.notebook, args.project_id)
