"""Retained sources, explicit run identity, submissions, and complete log retrieval."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from sugarloaf.adapters.quantconnect.cloud.backtests import (
    main,
    persist_bundle,
    read_all_logs,
    run_snapshot,
)
from sugarloaf.adapters.quantconnect.cloud.packaging import build_bundle, read_spec
from sugarloaf.deployment.evidence import read_snapshot, save_snapshot, write_json


def native_manifest(tmp_path):
    source = tmp_path / "reference.py"
    source.write_text("# Original source\n")
    manifest = tmp_path / "reference.toml"
    manifest.write_text('name = "reference"\nentrypoint = "reference.py"\nproject_id = 1\n')
    return manifest, source


def test_bundle_snapshots_preserve_actual_old_source_and_manifest(tmp_path):
    manifest, source = native_manifest(tmp_path)
    spec = read_spec(manifest)
    original = build_bundle(spec)
    saved = persist_bundle(spec.artifacts, original)
    source.write_text("# Edited dirty source\n")
    manifest.write_text(manifest.read_text() + '\n[parameters]\nlookback = "20"\n')
    later = persist_bundle(spec.artifacts, build_bundle(read_spec(manifest)))
    assert saved != later
    old = read_snapshot(spec.artifacts / saved["source_snapshot"])
    assert old["files"][0]["content"] == "# Original source\n"
    old_bundle = read_snapshot(spec.artifacts / saved["bundle_snapshot"])
    later_bundle = read_snapshot(spec.artifacts / later["bundle_snapshot"])
    assert old_bundle["manifest"] == original["manifest"]
    assert later_bundle["parameters"] == {"lookback": "20"}


def test_immutable_writes_publish_whole_files_and_reject_conflicting_updates(tmp_path):
    path = tmp_path / "attempt.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: write_json(path, {"large": "x" * 10000}, immutable=True), range(8)))
    assert json.loads(path.read_text()) == {"large": "x" * 10000}
    with pytest.raises(ValueError, match="Immutable evidence differs"):
        write_json(path, {"large": "replacement"}, immutable=True)
    with pytest.raises(ValueError):
        save_snapshot(tmp_path, {"metric": float("nan")})


def test_content_snapshot_detects_changed_source(tmp_path):
    snapshot = save_snapshot(tmp_path, {"source": "original"})
    snapshot.write_text('{"source": "tampered"}')
    with pytest.raises(ValueError, match="checksum mismatch"):
        read_snapshot(snapshot)


def test_explicit_snapshot_is_offline_and_never_selects_latest(tmp_path, monkeypatch, capsys):
    manifest, _ = native_manifest(tmp_path)
    artifacts = read_spec(manifest).artifacts
    saved = {"backtest_id": "old", "project_id": 7, "parameters": {"lookback": "10"}}
    write_json(artifacts / "old-deployment.json", saved, immutable=True)
    write_json(artifacts / "cloud.json", {"backtest_id": "new", "project_id": 1})
    monkeypatch.setattr(sys, "argv", ["cloud", "--manifest", str(manifest), "snapshot",
                                     "--backtest-id", "old"])
    main()
    assert json.loads(capsys.readouterr().out) == saved
    with pytest.raises(FileNotFoundError):
        run_snapshot(artifacts, "missing")
    with pytest.raises(ValueError, match="safe explicit"):
        run_snapshot(artifacts, "../other")


def test_log_pagination_retains_more_than_first_200_lines():
    lines = [f"line {number}" for number in range(450)]
    calls = []

    def post(endpoint, request):
        calls.append(request)
        return {"success": True, "length": len(lines),
                "logs": lines[request["start"]:request["end"]]}

    result = read_all_logs(SimpleNamespace(post=post), 7, "exact")
    assert result["logs"] == lines
    assert [(call["start"], call["end"]) for call in calls] == [(0, 200), (200, 400), (400, 600)]
    assert all(call["backtestId"] == "exact" for call in calls)


def test_log_retrieval_rejects_truncation_and_changing_lengths():
    with pytest.raises(ValueError, match="incomplete"):
        read_all_logs(SimpleNamespace(post=lambda *_: {"length": 201, "logs": []}), 7, "x")
    pages = iter([{"length": 201, "logs": ["x"] * 200}, {"length": 202, "logs": ["x", "y"]}])
    with pytest.raises(ValueError, match="length changed"):
        read_all_logs(SimpleNamespace(post=lambda *_: next(pages)), 7, "x")
    with pytest.raises(RuntimeError, match="retrieval failed"):
        read_all_logs(SimpleNamespace(post=lambda *_: {"success": False, "errors": ["denied"]}),
                      7, "x")


def fake_client(monkeypatch, post):
    module = ModuleType("lean.container")
    module.container = SimpleNamespace(api_client=SimpleNamespace(post=post))
    monkeypatch.setitem(sys.modules, "lean.container", module)


def test_failed_submission_records_intent_before_network(tmp_path, monkeypatch, capsys):
    manifest, _ = native_manifest(tmp_path)
    artifacts = read_spec(manifest).artifacts
    write_json(artifacts / "cloud.json", {"project_id": 1, "compile_id": "compiled"})

    def post(endpoint, request):
        attempts = list((artifacts / "submissions").glob("*.json"))
        assert len(attempts) == 1
        assert read_snapshot(attempts[0])["request"] == request
        raise ConnectionError("Response lost after submission")

    fake_client(monkeypatch, post)
    monkeypatch.setattr(sys, "argv", ["cloud", "--manifest", str(manifest), "backtest"])
    assert main() == 1
    assert "ConnectionError: Response lost" in capsys.readouterr().err
    outcomes = list((artifacts / "submission-outcomes").glob("*.json"))
    assert read_snapshot(outcomes[0])["status"] == "submission_uncertain"
    assert not list(artifacts.glob("*-deployment.json"))


def test_status_uses_explicit_saved_project_and_retains_each_observation(tmp_path, monkeypatch):
    manifest, _ = native_manifest(tmp_path)
    artifacts = read_spec(manifest).artifacts
    write_json(artifacts / "cloud.json", {"project_id": 1, "backtest_id": "new"})
    write_json(artifacts / "old-deployment.json", {"project_id": 7, "backtest_id": "old"},
               immutable=True)
    observed = iter([False, True])

    def post(endpoint, request):
        assert request == {"projectId": 7, "backtestId": "old"}
        return {"backtest": {"backtestId": "old", "completed": next(observed)}}

    fake_client(monkeypatch, post)
    monkeypatch.setattr(sys, "argv", ["cloud", "--manifest", str(manifest), "status",
                                     "--backtest-id", "old"])
    main()
    main()
    assert len(list((artifacts / "observations" / "old").glob("*.json"))) == 2
    assert json.loads((artifacts / "cloud.json").read_text())["backtest_id"] == "new"
