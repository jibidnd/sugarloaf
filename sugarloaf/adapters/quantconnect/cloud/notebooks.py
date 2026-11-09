"""Execute a local notebook on QC's remote Jupyter kernel, preserving outputs.

Connects through :mod:`.kernels` (QC's research endpoints, which are not part of
its official API) with the REST client in :mod:`.client`. Needs nbformat and
websocket-client. Connection tokens stay in memory. Only the newly created
kernel is shut down.
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from .client import default_client
from .kernels import connect, run_cell, start_kernel, stop_kernel


def run_notebook(path: Path, project_id: int, timeout: int = 600, *, client=None):
    # nbformat is loaded only for this remote notebook tool; ordinary graph
    # execution and offline packaging do not need a Jupyter client.
    import nbformat

    server = connect(client or default_client(), project_id)
    notebook = nbformat.read(path, as_version=4)
    # Clear previous code outputs in memory before execution so retained outputs
    # describe this attempt rather than silently mixing new cells with old results.
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs, cell.execution_count = [], None
    # Own a new kernel for this invocation. Cleanup deletes only this kernel ID,
    # not other notebooks or the entire shared research server.
    kernel_id = start_kernel(server)
    notebook.metadata["quantconnect_run"] = {
        "project_id": project_id,
        "kernel": "python3",
        "started_utc": datetime.now(UTC).isoformat(),
        "completed": False,
    }

    def echo(output):
        if output["output_type"] == "stream":
            print(output["text"], end="", flush=True)
        elif output["output_type"] == "error":
            print(output["ename"] + ": " + output["evalue"], flush=True)

    try:
        for index, cell in enumerate(notebook.cells):
            if cell.cell_type != "code":
                continue
            print(f"Executing cell {index + 1}/{len(notebook.cells)}", flush=True)
            # The kernel is deleted below, so a timeout need not interrupt it first.
            result = run_cell(
                server, kernel_id, cell.source, timeout=timeout, on_output=echo,
                interrupt_on_timeout=False,
            )
            cell.outputs = [nbformat.from_dict(output) for output in result.outputs]
            cell.execution_count = result.execution_count
            # Retain progress cell by cell, including errors, so a failed attempt still
            # has inspectable output rather than losing all evidence at the final cell.
            nbformat.write(notebook, path)
            if result.status == "timeout":
                raise TimeoutError(f"Cell {index + 1} exceeded {timeout}s")
            if result.status == "error":
                raise RuntimeError(f"Notebook cell {index + 1} failed; outputs saved")
        notebook.metadata["quantconnect_run"].update(
            completed=True, finished_utc=datetime.now(UTC).isoformat()
        )
        print(f"Completed notebook: {path}", flush=True)
    # Persist partial results and close the owned kernel even when a cell fails.
    # The notebook itself is evidence; connection tokens are not written into it.
    finally:
        nbformat.write(notebook, path)
        stop_kernel(server, kernel_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", type=Path)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per cell")
    args = parser.parse_args()
    run_notebook(args.notebook, args.project_id, args.timeout)
