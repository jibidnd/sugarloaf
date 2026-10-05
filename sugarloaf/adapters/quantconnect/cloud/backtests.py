"""Local QC project operations and configuration-driven backtest runs.

Uses the authenticated LEAN client with its standard network behavior. Local
source/result records identify submitted inputs; they are not a cloud lock or
runtime restart mechanism.
"""

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sugarloaf.deployment.evidence import read_snapshot, save_snapshot, write_json

from .packaging import BundleSpec, build_bundle, read_spec


def _client(client=None):
    # Dependency injection lets local tests supply a fake API without loading LEAN.
    # Real runs use the native client and its stored authentication/network behavior.
    if client is None:
        from lean.container import container

        return container.api_client
    return client


def _state(spec: BundleSpec) -> dict:
    # cloud.json is the current selection pointer, not immutable historical proof.
    # An explicit manifest project cannot silently disagree with its saved project.
    path = spec.artifacts / "cloud.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if spec.project_id:
        if state.get("project_id", spec.project_id) != spec.project_id:
            raise ValueError("Manifest project differs from saved deployment state")
        state["project_id"] = spec.project_id
    return state


def _required(state: dict, key: str):
    # Fail before a remote action if its prerequisite identity has not been selected.
    # For example, run needs an existing project rather than creating one implicitly.
    if not state.get(key):
        raise ValueError(f"Select {key} before this operation")
    return state[key]


def _save_state(spec: BundleSpec, state: dict) -> None:
    # Advance the current pointer atomically; historical bundles and per-run
    # records remain separate so later uploads cannot erase their identities.
    write_json(spec.artifacts / "cloud.json", state)


def persist_bundle(artifacts: Path, bundle: dict) -> dict:
    """Retain immutable sources/configuration plus a convenient bundle.json copy."""
    # Retain source-only and full-config snapshots separately: parameter changes
    # can share code identity while having different effective run configurations.
    sources = {key: bundle[key] for key in ("files", "source_manifest", "source_hash")}
    source_path = save_snapshot(artifacts / "sources", sources)
    bundle_path = save_snapshot(artifacts / "bundles", bundle)
    write_json(artifacts / "bundle.json", bundle)
    return {
        "source_snapshot": source_path.relative_to(artifacts).as_posix(),
        "bundle_snapshot": bundle_path.relative_to(artifacts).as_posix(),
    }


def package(spec: BundleSpec, *, parameters: Mapping[str, str] | None = None) -> dict:
    """Build and retain selected source/configuration offline, including overrides."""
    # Packaging is offline and does not call the deployment factory. Overrides
    # are merged before retaining the effective configuration snapshot.
    bundle = build_bundle(spec)
    bundle["parameters"] = {**spec.parameters, **dict(parameters or {})}
    evidence = persist_bundle(spec.artifacts, bundle)
    return {"bundle": bundle, **evidence}


def create_project(spec: BundleSpec, *, client=None) -> dict:
    """Create one QC project explicitly and retain its selected identity."""
    # Project creation is an explicit setup action. Reusing the saved selection
    # keeps ordinary strategy runs from allocating fresh cloud projects repeatedly.
    state = _state(spec)
    if state.get("project_id"):
        raise ValueError("Project already selected; reuse it")
    result = _client(client).post("projects/create", {"name": spec.name, "language": "Py"})
    state["project_id"] = result["projects"][0]["projectId"]
    _save_state(spec, state)
    return {"project_id": state["project_id"]}


def upload(spec: BundleSpec, *, parameters: Mapping[str, str] | None = None, client=None) -> dict:
    """Package and upload selected sources/parameters; clear prior compile/run selection."""
    # Capture the actual sources before network activity. Send source files and
    # parameters together through the native project client.
    state = _state(spec)
    project_id = _required(state, "project_id")
    prepared = package(spec, parameters=parameters)
    bundle = prepared.pop("bundle")
    # QC may accept an upload even if its response is lost. Do not let a later
    # compile or submission attribute that project to the previous source bundle.
    for key in ("source_hash", "compile_id", "backtest_id"):
        state.pop(key, None)
    _save_state(spec, state)
    # The upload boundary: LEAN owns request authentication and native retries.
    # Sugarloaf does not implement a second HTTP transport layer.
    _client(client).projects.update(
        project_id, files=bundle["files"], parameters=bundle["parameters"]
    )
    state.update({key: value for key, value in bundle.items() if key != "files"})
    state.update(prepared)
    _save_state(spec, state)
    return {"uploaded_files": len(bundle["files"]), "parameters": state["parameters"]}


def set_parameters(spec: BundleSpec, parameters: Mapping[str, str], *, client=None) -> dict:
    """Update project parameters and retain the effective values for submission."""
    # This action updates the current project selection. A later upload/run starts
    # again from manifest defaults plus that call's overrides, not stale project values.
    if not parameters:
        raise ValueError("parameters requires --parameter NAME VALUE")
    state = _state(spec)
    project_id = _required(state, "project_id")
    effective = {**spec.parameters, **state.get("parameters", {}), **dict(parameters)}
    _client(client).projects.update(project_id, parameters=effective)
    state["parameters"] = effective
    _save_state(spec, state)
    return effective


def compile_project(spec: BundleSpec, *, client=None) -> dict:
    """Request compilation of the selected uploaded project and retain its compile ID."""
    # Only confirmed uploaded source identity authorizes compilation attribution.
    # The provider returns a compile ID immediately; it may still be queued/building.
    state = _state(spec)
    _required(state, "source_hash")
    project_id = _required(state, "project_id")
    result = _client(client).post("compile/create", {"projectId": project_id})
    state["compile_id"] = result["compileId"]
    _save_state(spec, state)
    return result


def compile_status(spec: BundleSpec, *, client=None) -> dict:
    """Read and retain the selected compile with its uploaded source identity."""
    # Retain every observation with source identity, including failed builds.
    # A request being accepted is not equivalent to BuildSuccess.
    state = _state(spec)
    request = {
        "projectId": _required(state, "project_id"),
        "compileId": _required(state, "compile_id"),
    }
    result = _client(client).get("compile/read", request)
    save_snapshot(spec.artifacts / "compilations", {"deployment": state, "compile": result})
    return result


def submit_backtest(spec: BundleSpec, *, name: str | None = None, client=None) -> dict:
    """Submit the selected compile with explicit parameters and retain request/outcome."""
    # Freeze submission intent before sending the request, including explicit
    # effective parameters rather than relying solely on mutable project settings.
    state = _state(spec)
    request = {
        "projectId": _required(state, "project_id"),
        "compileId": _required(state, "compile_id"),
        "backtestName": name or spec.name,
        "parameters": state.get("parameters", {}),
    }
    # If the response is lost, this record still shows what we tried to submit.
    # It is evidence of intent, not a guarantee of exactly-once cloud submission.
    submission = save_snapshot(
        spec.artifacts / "submissions",
        {
            "requested_utc": datetime.now(UTC).isoformat(),
            "deployment": state,
            "request": request,
        },
    )
    try:
        # Compilation and submission are separate API operations. The returned ID
        # identifies a remote backtest whose completion must still be observed.
        result = _client(client).post("backtests/create", request)["backtest"]
    except Exception as error:
        save_snapshot(
            spec.artifacts / "submission-outcomes",
            {
                "submission": submission.relative_to(spec.artifacts).as_posix(),
                "status": "submission_uncertain",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        raise
    save_snapshot(
        spec.artifacts / "submission-outcomes",
        {
            "submission": submission.relative_to(spec.artifacts).as_posix(),
            "status": "accepted",
            "backtest": result,
        },
    )
    state["submission_snapshot"] = submission.relative_to(spec.artifacts).as_posix()
    state["backtest_id"] = result["backtestId"]
    write_json(spec.artifacts / f"{state['backtest_id']}-deployment.json", state, immutable=True)
    _save_state(spec, state)
    return {"backtest_id": state["backtest_id"]}


def run_snapshot(artifacts: Path, backtest_id: str) -> dict:
    """Load one retained run offline and check its referenced source identities."""
    # Historical retrieval uses the requested run's saved identity even after
    # cloud.json points to a newer upload or run.
    if not backtest_id or not re.fullmatch(r"[A-Za-z0-9_-]+", backtest_id):
        raise ValueError("A safe explicit backtest ID is required")
    path = artifacts / f"{backtest_id}-deployment.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("backtest_id") != backtest_id:
        raise ValueError("Saved deployment does not match the requested backtest")
    for key in ("source_snapshot", "bundle_snapshot"):
        if key in snapshot:
            evidence = (artifacts / snapshot[key]).resolve()
            if not evidence.is_relative_to(artifacts.resolve()) or not evidence.is_file():
                raise ValueError(f"Saved run has missing or unsafe {key}")
            if read_snapshot(evidence).get("source_hash") != snapshot.get("source_hash"):
                raise ValueError(f"Saved run source hash differs from {key}")
    return snapshot


def _selected_run(spec: BundleSpec, backtest_id: str | None) -> dict:
    # An explicit ID overrides the current pointer; both paths resolve retained
    # run evidence rather than guessing which remote backtest belongs to these sources.
    selected = backtest_id or _required(_state(spec), "backtest_id")
    return run_snapshot(spec.artifacts, selected)


def read_status(spec: BundleSpec, *, backtest_id: str | None = None, client=None) -> dict:
    """Retrieve and retain status/statistics for the selected saved run."""
    # Use the historical project/run pair, then preserve each status observation
    # before updating the convenient latest result file.
    deployment = _selected_run(spec, backtest_id)
    selected = deployment["backtest_id"]
    result = _client(client).post(
        "backtests/read",
        {
            "projectId": deployment["project_id"],
            "backtestId": selected,
        },
    )["backtest"]
    record = {"deployment": deployment, "backtest": result}
    observed = save_snapshot(spec.artifacts / "observations" / selected, record)
    write_json(spec.artifacts / f"{selected}.json", record)
    return {**result, "evidence": str(observed)}


def read_all_logs(client, project_id: int, backtest_id: str) -> dict:
    """Read QC's 200-line log pages; reject incomplete pages or changing lengths."""
    # Advance by the number actually received. Requiring a stable total and full
    # pages prevents a partial retrieval from being reported as complete evidence.
    logs = []
    length = None
    while length is None or len(logs) < length:
        result = client.post(
            "backtests/read/log",
            {
                "projectId": project_id,
                "backtestId": backtest_id,
                "start": len(logs),
                "end": len(logs) + 200,
                "query": "",
            },
        )
        if result.get("success") is False or result.get("errors"):
            raise RuntimeError(f"Log retrieval failed: {result.get('errors')}")
        reported, page = result.get("length"), result.get("logs")
        if not isinstance(reported, int) or reported < 0 or not isinstance(page, list):
            raise ValueError("Log response lacks valid length/logs")
        if length is not None and reported != length:
            raise ValueError("Log length changed during retrieval; retry after completion")
        length = reported
        if len(page) != min(200, length - len(logs)):
            raise ValueError("Log page is incomplete or exceeds its declared length")
        logs.extend(page)
    return {"logs": logs, "length": length, "success": True, "errors": []}


def read_logs(spec: BundleSpec, *, backtest_id: str | None = None, client=None) -> dict:
    """Retrieve and retain available logs using the saved run's project identity."""
    # Logs belong to the selected saved run, not necessarily the currently compiled
    # project sources. Store immutable observations as well as the convenient log file.
    deployment = _selected_run(spec, backtest_id)
    selected = deployment["backtest_id"]
    result = read_all_logs(_client(client), deployment["project_id"], selected)
    save_snapshot(spec.artifacts / "observations" / selected, {"logs": result})
    write_json(spec.artifacts / f"{selected}-logs.json", result)
    return result


def _wait(read, complete, timeout: float, poll_interval: float, label: str) -> dict:
    # Use a monotonic local clock for polling duration; wall-clock adjustments
    # must not extend a wait. These bounds do not interrupt an in-flight SDK request.
    deadline = time.monotonic() + timeout
    while True:
        result = read()
        if complete(result):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {label}; selected IDs remain in cloud.json")
        time.sleep(min(poll_interval, remaining))


def run_backtest(
    manifest: str | Path,
    *,
    parameters: Mapping[str, str] | None = None,
    name: str | None = None,
    compile_timeout: float = 120,
    backtest_timeout: float = 1800,
    poll_interval: float = 5,
    client=None,
) -> dict:
    """Run a selected config through upload, compilation, backtest and evidence retrieval.

    Requires an existing selected project. Waiting limits bound polling between
    LEAN requests, not the client's network calls. A timeout does not cancel a
    remote job. Native retries and authentication remain LEAN-owned.
    """
    # This is local orchestration, not the algorithm execution loop. QC initializes
    # uploaded main.py: a Sugarloaf graph in factory mode, or the selected native study.
    for label, value in (
        ("compile_timeout", compile_timeout),
        ("backtest_timeout", backtest_timeout),
        ("poll_interval", poll_interval),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be finite and positive")
    spec = read_spec(Path(manifest))
    upload(spec, parameters=parameters, client=client)
    compile_project(spec, client=client)

    def compiled(result):
        # Do not submit against a merely accepted or still-building compile. Native
        # initialization/runtime errors can still occur after a successful compile.
        if result["state"] == "BuildSuccess":
            return True
        if result["state"] not in {"InQueue", "Building"}:
            raise RuntimeError(f"QC compilation did not succeed: {json.dumps(result)}")
        return False

    _wait(
        lambda: compile_status(spec, client=client),
        compiled,
        compile_timeout,
        poll_interval,
        "compilation",
    )
    submitted = submit_backtest(spec, name=name, client=client)
    selected = submitted["backtest_id"]

    def finished(result):
        if result.get("error") or result.get("hasInitializeError"):
            return True
        # QC can mark completion before publishing metrics. Keep polling while
        # statistics is empty; zero-trade runs still publish their account metrics.
        # Errors above finish immediately so their logs can be retained and surfaced.
        statistics = result.get("statistics")
        return bool(result.get("completed")) and isinstance(statistics, dict) and bool(statistics)

    result = _wait(
        lambda: read_status(spec, backtest_id=selected, client=client),
        finished,
        backtest_timeout,
        poll_interval,
        f"backtest {selected}",
    )
    failure = None
    if result.get("error") or result.get("hasInitializeError"):
        error = result.get("error") or result.get("status") or "initialization error"
        failure = f"QC backtest {selected} failed: {error}"
    try:
        read_logs(spec, backtest_id=selected, client=client)
    except Exception as log_error:
        if failure:
            raise RuntimeError(f"{failure}; log retrieval failed: {log_error}") from log_error
        raise
    # Successfully retrieving logs does not turn an algorithm error into success.
    # The caller must see the run failure even when its evidence was collected.
    if failure:
        raise RuntimeError(failure)
    state = run_snapshot(spec.artifacts, selected)
    return {
        "project_id": state["project_id"],
        "compile_id": state["compile_id"],
        "backtest_id": selected,
        "source_hash": state["source_hash"],
        "parameters": state["parameters"],
        "completed": result["completed"],
        "statistics": result["statistics"],
        "artifacts": str(spec.artifacts),
    }


def main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """Run one local QC action; print one JSON result and surface errors."""
    # The shared CLI forwards here without opening the manifest. This parser owns
    # QC actions and option validation, then prints one machine-readable JSON result.
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "run",
            "package",
            "create",
            "upload",
            "parameters",
            "compile",
            "compile-status",
            "backtest",
            "status",
            "logs",
            "snapshot",
        ),
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--parameter", nargs=2, action="append", default=[], metavar=("NAME", "VALUE")
    )
    parser.add_argument("--backtest-id")
    parser.add_argument("--name")
    parser.add_argument("--compile-timeout", type=float, default=120)
    parser.add_argument("--backtest-timeout", type=float, default=1800)
    parser.add_argument("--poll-interval", type=float, default=5)
    args = parser.parse_args(argv)
    if args.name is not None and args.action not in {"run", "backtest"}:
        parser.error("--name applies to run and backtest")
    if args.parameter and args.action not in {"run", "package", "upload", "parameters"}:
        parser.error("--parameter applies to run, package, upload and parameters")
    if args.action != "run" and (
        args.compile_timeout != 120 or args.backtest_timeout != 1800 or args.poll_interval != 5
    ):
        parser.error("polling options apply to run")
    if args.backtest_id and args.action not in {"status", "logs", "snapshot"}:
        parser.error("--backtest-id applies to status, logs and snapshot")
    # Repeated --parameter NAME VALUE pairs become string overrides. Their effect
    # depends on whether the selected factory/native study actually reads each name.
    parameters = dict(args.parameter)
    try:
        spec = read_spec(args.manifest)
        if args.action == "run":
            print(f"Running {spec.name}; evidence: {spec.artifacts}", file=sys.stderr)
            result = run_backtest(
                args.manifest,
                parameters=parameters,
                name=args.name,
                compile_timeout=args.compile_timeout,
                backtest_timeout=args.backtest_timeout,
                poll_interval=args.poll_interval,
            )
        elif args.action == "package":
            prepared = package(spec, parameters=parameters)
            bundle = prepared.pop("bundle")
            result = {
                "bundle": str(spec.artifacts / "bundle.json"),
                "files": len(bundle["files"]),
                "source_hash": bundle["source_hash"],
                **prepared,
            }
        elif args.action == "create":
            result = create_project(spec)
        elif args.action == "upload":
            result = upload(spec, parameters=parameters)
        elif args.action == "parameters":
            result = set_parameters(spec, parameters)
        elif args.action == "compile":
            result = compile_project(spec)
        elif args.action == "compile-status":
            result = compile_status(spec)
        elif args.action == "backtest":
            result = submit_backtest(spec, name=args.name)
        elif args.action == "status":
            result = read_status(spec, backtest_id=args.backtest_id)
        elif args.action == "logs":
            result = read_logs(spec, backtest_id=args.backtest_id)
        else:
            if not args.backtest_id:
                parser.error("snapshot requires --backtest-id")
            result = run_snapshot(spec.artifacts, args.backtest_id)
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
