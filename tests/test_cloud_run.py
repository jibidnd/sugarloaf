"""Config-driven orchestration, effective parameters, and observable failed runs."""

import json
from types import SimpleNamespace

import pytest

from sugarloaf.adapters.quantconnect.cloud import backtests
from sugarloaf.adapters.quantconnect.cloud.backtests import run_backtest
from sugarloaf.adapters.quantconnect.cloud.packaging import read_spec
from sugarloaf.cli import main
from sugarloaf.deployment.evidence import read_snapshot, write_json


@pytest.fixture
def manifest(tmp_path):
    (tmp_path / "algorithm.py").write_text("# Native QC example\n")
    manifest = tmp_path / "strategy.toml"
    manifest.write_text(
        'name = "study"\nentrypoint = "algorithm.py"\nproject_id = 7\n'
        '[parameters]\nlookback = "10"\nbudget = "0.5"\n'
    )
    return manifest


class QCClient:
    """A client boundary with queued responses and visible submitted inputs."""

    def __init__(self):
        self.calls = []
        self.compiles = iter([{"state": "InQueue"}, {"state": "BuildSuccess"}])
        self.statuses = iter([
            {"completed": False, "statistics": None},
            {"completed": True, "statistics": {"Return": "2%"}},
        ])
        self.submission_error = None
        self.projects = SimpleNamespace(update=self.update)

    def update(self, project_id, **kwargs):
        self.calls.append(("update", {"projectId": project_id, **kwargs}))

    def get(self, endpoint, request):
        assert endpoint == "compile/read"
        self.calls.append((endpoint, request))
        return next(self.compiles)

    def post(self, endpoint, request):
        self.calls.append((endpoint, request))
        if endpoint == "compile/create":
            return {"compileId": "compiled"}
        if endpoint == "backtests/create":
            if self.submission_error:
                raise self.submission_error
            return {"backtest": {"backtestId": "exact"}}
        if endpoint == "backtests/read":
            return {"backtest": {"backtestId": "exact", **next(self.statuses)}}
        if endpoint == "backtests/read/log":
            return {"length": 2, "logs": ["started", "finished"]}
        raise AssertionError(endpoint)


@pytest.fixture
def client(monkeypatch):
    client = QCClient()
    monkeypatch.setattr(backtests, "_client", lambda provided=None: provided or client)
    monkeypatch.setattr(backtests.time, "sleep", lambda _: None)
    return client


def test_public_run_retains_effective_parameters_sources_results_and_logs(manifest, client):
    result = run_backtest(manifest, parameters={"lookback": "20"}, name="candidate")
    assert result["completed"] is True
    assert result["parameters"] == {"lookback": "20", "budget": "0.5"}
    assert result["statistics"] == {"Return": "2%"}
    upload = client.calls[0][1]
    assert upload["parameters"] == result["parameters"]
    submissions = [request for endpoint, request in client.calls if endpoint == "backtests/create"]
    assert submissions == [{
        "projectId": 7, "compileId": "compiled", "backtestName": "candidate",
        "parameters": result["parameters"],
    }]
    artifacts = read_spec(manifest).artifacts
    snapshot = backtests.run_snapshot(artifacts, "exact")
    assert snapshot["source_hash"] == result["source_hash"]
    retained = read_snapshot(artifacts / snapshot["bundle_snapshot"])
    assert retained["parameters"] == result["parameters"]
    assert retained["manifest"] == manifest.read_text()
    assert json.loads((artifacts / "exact-logs.json").read_text())["logs"] == [
        "started", "finished",
    ]
    assert len(list((artifacts / "observations/exact").glob("*.json"))) == 3


def test_config_choice_and_overrides_need_no_runtime_edits(manifest, client):
    source_before = (manifest.parent / "algorithm.py").read_bytes()
    first = run_backtest(manifest)
    another = manifest.with_name("another.toml")
    another.write_text(manifest.read_text().replace('lookback = "10"', 'lookback = "30"'))
    client.compiles = iter([{"state": "BuildSuccess"}])
    client.statuses = iter([{"completed": True, "statistics": {"Total Orders": "0"}}])
    second = run_backtest(another)
    assert first["source_hash"] == second["source_hash"]
    assert first["parameters"]["lookback"] == "10"
    assert second["parameters"]["lookback"] == "30"
    assert first["artifacts"] != second["artifacts"]
    assert (manifest.parent / "algorithm.py").read_bytes() == source_before


def test_completed_run_waits_for_metrics_and_retains_intermediate_status(manifest, client):
    # The provider can publish completion first and fill statistics on a later read.
    client.statuses = iter([
        {"completed": True, "statistics": {}},
        {"completed": True, "statistics": {"Total Orders": "0", "End Equity": "100000"}},
    ])
    result = run_backtest(manifest, client=client)
    assert result["statistics"] == {"Total Orders": "0", "End Equity": "100000"}
    assert len([call for call in client.calls if call[0] == "backtests/create"]) == 1
    assert len([call for call in client.calls if call[0] == "backtests/read"]) == 2
    observations = []
    for path in (read_spec(manifest).artifacts / "observations/exact").glob("*.json"):
        observed = read_snapshot(path)
        if "backtest" in observed:
            observations.append(observed["backtest"])
    assert {json.dumps(row["statistics"], sort_keys=True) for row in observations} == {
        "{}", json.dumps(result["statistics"], sort_keys=True),
    }


def test_compile_error_does_not_submit_backtest(manifest, client):
    client.compiles = iter([{"state": "BuildError", "logs": ["bad import"]}])
    with pytest.raises(RuntimeError, match="bad import"):
        run_backtest(manifest)
    assert not any(endpoint == "backtests/create" for endpoint, _ in client.calls)
    state = json.loads((read_spec(manifest).artifacts / "cloud.json").read_text())
    assert state["compile_id"] == "compiled"
    evidence = next((read_spec(manifest).artifacts / "compilations").glob("*.json"))
    observed = read_snapshot(evidence)
    assert observed["compile"]["logs"] == ["bad import"]
    assert observed["deployment"]["source_hash"] == state["source_hash"]


def test_algorithm_error_is_preserved_when_log_collection_fails(manifest, client, monkeypatch):
    client.statuses = iter([{"completed": False, "error": "bad data"}])

    def unavailable(*_):
        raise ConnectionError("logs unavailable")

    monkeypatch.setattr(backtests, "read_all_logs", unavailable)
    with pytest.raises(RuntimeError, match="bad data; log retrieval failed: logs unavailable"):
        run_backtest(manifest)
    result = json.loads((read_spec(manifest).artifacts / "exact.json").read_text())
    assert result["backtest"]["error"] == "bad data"


@pytest.mark.parametrize("result", [
    {"completed": False, "error": "bad data"},
    {"completed": False, "hasInitializeError": True},
    {"completed": True, "statistics": {}, "error": "bad data"},
    {"completed": True, "statistics": {}, "hasInitializeError": True},
])
def test_algorithm_failure_retains_error_and_logs(manifest, client, result):
    client.statuses = iter([result])
    with pytest.raises(RuntimeError, match="failed"):
        run_backtest(manifest)
    artifacts = read_spec(manifest).artifacts
    assert (artifacts / "exact-logs.json").exists()
    assert json.loads((artifacts / "exact.json").read_text())["backtest"]["completed"] == (
        result["completed"]
    )


def test_failed_submission_is_observable_and_not_repeated_by_runner(manifest, client):
    client.submission_error = ConnectionError("Response lost")
    with pytest.raises(ConnectionError, match="Response lost"):
        run_backtest(manifest)
    artifacts = read_spec(manifest).artifacts
    outcome = next((artifacts / "submission-outcomes").glob("*.json"))
    assert read_snapshot(outcome)["status"] == "submission_uncertain"
    assert len([call for call in client.calls if call[0] == "backtests/create"]) == 1
    assert not any(call[0] == "backtests/read" for call in client.calls)


@pytest.mark.parametrize("stage", ["compile", "backtest"])
def test_poll_timeout_retains_selected_job_without_cancelling(manifest, client, monkeypatch, stage):
    clock = iter([0.0, 2.0])
    if stage == "backtest":
        client.compiles = iter([{"state": "BuildSuccess"}])
        clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(backtests.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="cloud.json"):
        run_backtest(manifest, compile_timeout=1, backtest_timeout=1)
    state = json.loads((read_spec(manifest).artifacts / "cloud.json").read_text())
    assert state["compile_id"] == "compiled"
    assert (state.get("backtest_id") == "exact") == (stage == "backtest")
    assert not any("cancel" in endpoint for endpoint, _ in client.calls)


def test_missing_project_fails_before_client_or_upload(manifest, monkeypatch):
    manifest.write_text(manifest.read_text().replace("project_id = 7\n", ""))
    monkeypatch.setattr(backtests, "_client", lambda *_: pytest.fail("No client needed"))
    with pytest.raises(ValueError, match="project_id"):
        run_backtest(manifest)


def test_interrupted_upload_cannot_attribute_new_remote_source_to_old_bundle(manifest, client):
    spec = read_spec(manifest)
    backtests.upload(spec, client=client)
    original = json.loads((spec.artifacts / "cloud.json").read_text())
    original.update(compile_id="old-compile", backtest_id="old-run")
    write_json(spec.artifacts / "cloud.json", original)
    write_json(spec.artifacts / "old-run-deployment.json", original, immutable=True)
    (manifest.parent / "algorithm.py").write_text("# Different source\n")

    def accepted_but_response_lost(project_id, **kwargs):
        assert kwargs["files"] == [{"name": "main.py", "content": "# Different source\n"}]
        raise ConnectionError("Upload response lost")

    client.projects.update = accepted_but_response_lost
    with pytest.raises(ConnectionError, match="Upload response lost"):
        backtests.upload(spec, client=client)
    with pytest.raises(ValueError, match="source_hash"):
        backtests.compile_project(spec, client=client)
    with pytest.raises(ValueError, match="compile_id"):
        backtests.submit_backtest(spec, client=client)
    retained = backtests.run_snapshot(spec.artifacts, "old-run")
    assert retained["source_hash"] == original["source_hash"]
    state = json.loads((spec.artifacts / "cloud.json").read_text())
    assert state["project_id"] == 7
    assert "backtest_id" not in state
    client.projects.update = client.update
    backtests.upload(spec, client=client)
    backtests.compile_project(spec, client=client)
    assert json.loads((spec.artifacts / "cloud.json").read_text())["source_hash"] != (
        original["source_hash"]
    )


@pytest.mark.parametrize("option,value", [
    ("compile_timeout", 0), ("backtest_timeout", -1), ("poll_interval", float("nan")),
])
def test_invalid_poll_limits_fail_before_upload(manifest, client, option, value):
    with pytest.raises(ValueError, match="finite and positive"):
        run_backtest(manifest, **{option: value})
    assert client.calls == []


def test_console_run_json_and_failure_exit(manifest, client, capsys):
    assert main(["run", "--adapter", "quantconnect", "--manifest", str(manifest),
                 "--parameter", "lookback", "40"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["parameters"]["lookback"] == "40"
    assert "Running study" in captured.err
    client.compiles = iter([{"state": "BuildError"}])
    assert main(["run", "--adapter", "quantconnect", "--manifest", str(manifest)]) == 1
    assert "RuntimeError:" in capsys.readouterr().err


@pytest.mark.parametrize("options", [
    ["package", "--name", "ignored"],
    ["compile", "--parameter", "lookback", "20"],
    ["upload", "--backtest-id", "exact"],
    ["package", "--poll-interval", "1"],
])
def test_cli_rejects_options_that_do_not_apply(manifest, options):
    with pytest.raises(SystemExit) as error:
        main([*options, "--adapter", "quantconnect", "--manifest", str(manifest)])
    assert error.value.code == 2
