"""tools/native_checks.py: check-file validation, statistic comparison and output records.

Every test passes ``main`` a replacement for ``run_backtest`` that returns the statistics the
test lists, so no test contacts QuantConnect.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tools import native_checks
from tools.native_checks import compare, main, parse_number

REPO = Path(__file__).resolve().parents[1]

# Statistics as QuantConnect returned them for backtest 039f9445ad7a0447bbc6e56f35285f67.
EMA_STATISTICS = {"Total Orders": "6", "Total Fees": "$6.00", "End Equity": "99665.11",
                  "Net Profit": "-0.335%", "Lowest Capacity Asset": "SPY R735QTJ8XC9X"}
NO_ORDERS = {"Total Orders": "0", "Total Fees": "$0.00", "End Equity": "100000",
             "Net Profit": "0%"}


class ScriptedBacktests:
    """A ``run_backtest`` replacement: returns the listed result per manifest stem, in order.

    A listed exception is raised instead of returned. Calls are kept for assertions.
    """

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, manifest, *, name, backtest_timeout):
        self.calls.append((Path(manifest).stem, name, backtest_timeout))
        result = self.results[Path(manifest).stem]
        if isinstance(result, BaseException):
            raise result
        return {"backtest_id": f"bt-{Path(manifest).stem}", "project_id": 7,
                "compile_id": "c", "source_hash": "h", "parameters": {},
                "completed": True, "statistics": result}


def write_manifest(folder: Path, stem: str, project_id: int | None = 7) -> None:
    project = f"project_id = {project_id}\n" if project_id else ""
    (folder / f"{stem}.toml").write_text(
        f'name = "{stem}"\nentrypoint = "algorithm.py"\n{project}', encoding="utf-8"
    )


def write_checks(folder: Path, checks, stems=None) -> Path:
    (folder / "algorithm.py").write_text("# native study\n", encoding="utf-8")
    for stem in stems or {Path(c["manifest"]).stem for c in checks}:
        write_manifest(folder, stem)
    path = folder / "checks.json"
    path.write_text(json.dumps({"checks": checks}), encoding="utf-8")
    return path


THREE = [
    {"name": "ema", "manifest": "ema.toml",
     "expected": {"Total Orders": 6, "Total Fees": "$6.00", "End Equity": "99665.11"},
     "tolerance": {"End Equity": 50}},
    {"manifest": "flat.toml", "expected": {"Total Orders": 0}, "backtest_timeout": 60},
    {"manifest": "new.toml"},
]


@pytest.fixture(autouse=True)
def no_saved_login(monkeypatch, tmp_path):
    # Credentials come only from what a test sets, never from the machine running the tests.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("QC_USER_ID", raising=False)
    monkeypatch.delenv("QC_API_TOKEN", raising=False)


def test_parse_number_reads_quantconnect_statistics():
    assert parse_number("$6.00") == (Decimal("6.00"), "$")
    assert parse_number("99665.11") == (Decimal("99665.11"), "")
    assert parse_number("-0.335%") == (Decimal("-0.335"), "%")
    assert parse_number("$-334.89") == (Decimal("-334.89"), "$")
    assert parse_number("-$6.00") == (Decimal("-6.00"), "$")
    assert parse_number("$46,000,000.00") == (Decimal("46000000.00"), "$")
    assert parse_number("-0.33 %") == (Decimal("-0.33"), "%")
    assert parse_number(6) == (Decimal("6"), "")
    for text in ("SPY R735QTJ8XC9X", "", "$", "6$", True, None, float("nan")):
        assert parse_number(text) is None


def test_compare_total_orders_exactly_and_numbers_within_tolerance():
    assert compare("Total Orders", 6, "6", Decimal(0)).passed
    assert not compare("Total Orders", 6, "7", Decimal(0)).passed
    assert compare("End Equity", "99665.11", "99700", Decimal(50)).passed
    assert not compare("End Equity", "99665.11", "99715.12", Decimal(50)).passed
    assert compare("Total Fees", "$6.00", "$6", Decimal(0)).passed
    assert not compare("Total Fees", "$6.00", "$6.01", Decimal(0)).passed
    # A unit in the expectation must match; a bare number accepts either unit.
    assert not compare("Net Profit", "-0.335%", "$-0.335", Decimal(1)).passed
    assert compare("Net Profit", -0.335, "-0.335%", Decimal(0)).passed
    assert not compare("End Equity", "99665.11", None, Decimal(50)).passed
    assert not compare("End Equity", "99665.11", "n/a", Decimal(50)).passed
    assert compare("Lowest Capacity Asset", "SPY R735QTJ8XC9X", "SPY R735QTJ8XC9X",
                   Decimal(0)).passed


def test_checks_run_in_order_and_report_pass_fail_and_recorded(tmp_path, capsys):
    path = write_checks(tmp_path, THREE)
    out = tmp_path / "out" / "result.json"
    runs = ScriptedBacktests({
        "ema": {**EMA_STATISTICS, "End Equity": "99700.00"},
        "flat": {**NO_ORDERS, "Total Orders": "1"},
        "new": NO_ORDERS,
    })
    assert main([str(path), "--out", str(out)], run_backtest=runs) == 1
    assert [c[0] for c in runs.calls] == ["ema", "flat", "new"]
    assert [c[2] for c in runs.calls] == [1800.0, 60.0, 1800.0]
    assert all(c[1].startswith(f"Sugarloaf native check {c[0]} ") for c in runs.calls)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["ema", str(tmp_path / "ema.toml"), "bt-ema", "pass"]
    assert "End Equity 99700.00 vs 99665.11 (diff 34.89 tol 50) ok" in lines[0]
    assert lines[1].split()[3] == "fail" and "Total Orders 1 vs 0 (exact) FAIL" in lines[1]
    assert lines[2].split()[3] == "recorded" and "End Equity 100000" in lines[2]
    assert lines[3].startswith("summary: 1 pass, 1 fail, 1 recorded, 0 error")
    saved = json.loads(out.read_text())
    assert [c["status"] for c in saved["checks"]] == ["pass", "fail", "recorded"]
    assert saved["checks"][0]["backtest_id"] == "bt-ema"
    assert saved["checks"][0]["statistics"]["Net Profit"] == "-0.335%"
    assert saved["checks"][2]["comparisons"] == []
    assert saved["finished_utc"] and len(saved["check_file_sha256"]) == 64


def test_all_passing_exits_zero(tmp_path):
    path = write_checks(tmp_path, THREE[:2])
    runs = ScriptedBacktests({"ema": EMA_STATISTICS, "flat": NO_ORDERS})
    assert main([str(path), "--out", str(tmp_path / "r.json")], run_backtest=runs) == 0


def test_a_run_that_raises_is_reported_and_the_rest_still_run(tmp_path, capsys):
    path = write_checks(tmp_path, THREE[:2])
    runs = ScriptedBacktests({"ema": RuntimeError("QC backtest x failed: boom"),
                              "flat": NO_ORDERS})
    out = tmp_path / "r.json"
    assert main([str(path), "--out", str(out)], run_backtest=runs) == 1
    assert len(runs.calls) == 2
    text = capsys.readouterr().out
    assert "error    RuntimeError: QC backtest x failed: boom" in text
    saved = json.loads(out.read_text())
    assert saved["checks"][0]["error"].startswith("RuntimeError")
    assert saved["checks"][1]["status"] == "pass"


def test_credentials_never_reach_the_console_or_the_output_file(tmp_path, monkeypatch, capsys):
    token = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("QC_USER_ID", "424242")
    monkeypatch.setenv("QC_API_TOKEN", token)
    path = write_checks(tmp_path, THREE[:1])
    runs = ScriptedBacktests({"ema": ValueError(f"auth failed for 424242 with {token}")})
    out = tmp_path / "r.json"
    assert main([str(path), "--out", str(out)], run_backtest=runs) == 1
    captured = capsys.readouterr()
    for text in (captured.out, captured.err, out.read_text()):
        assert token not in text and "424242" not in text
    assert "[redacted]" in captured.out


def test_a_user_id_inside_a_statistic_is_kept(tmp_path, monkeypatch):
    # The user id is redacted from error messages only; statistics stay as QuantConnect gave them.
    monkeypatch.setenv("QC_USER_ID", "6")
    monkeypatch.setenv("QC_API_TOKEN", "0123456789abcdef0123456789abcdef")
    path = write_checks(tmp_path, THREE[:1])
    out = tmp_path / "r.json"
    assert main([str(path), "--out", str(out)],
                run_backtest=ScriptedBacktests({"ema": EMA_STATISTICS})) == 0
    assert json.loads(out.read_text())["checks"][0]["statistics"]["Total Orders"] == "6"


def test_dry_run_and_only_select_without_running(tmp_path, capsys):
    path = write_checks(tmp_path, THREE)
    runs = ScriptedBacktests({})
    assert main([str(path), "--dry-run"], run_backtest=runs) == 0
    assert runs.calls == []
    listing = capsys.readouterr().out
    assert "3 backtest(s) would run" in listing and "End Equity 99665.11 +/- 50" in listing
    assert main([str(path), "--dry-run", "--only", "flat"], run_backtest=runs) == 0
    assert "1 backtest(s) would run" in capsys.readouterr().out
    runs = ScriptedBacktests({"new": NO_ORDERS})
    assert main([str(path), "--only", "new", "--out", str(tmp_path / "r.json")],
                run_backtest=runs) == 0
    assert [c[0] for c in runs.calls] == ["new"]
    assert main([str(path), "--only", "missing", "--dry-run"], run_backtest=runs) == 2
    assert "no such check" in capsys.readouterr().err


@pytest.mark.parametrize(("check", "message"), [
    ({"manifest": "ema.toml", "expect": {}}, "unknown keys"),
    ({"manifest": "ema.toml", "expected": {"Total Orders": 6},
      "tolerance": {"Total Orders": 1}}, "compared exactly"),
    ({"manifest": "ema.toml", "tolerance": {"End Equity": 5}}, "no expectation"),
    ({"manifest": "ema.toml", "expected": {"End Equity": "1"},
      "tolerance": {"End Equity": -1}}, ">= 0"),
    ({"manifest": "ema.toml", "expected": {"Total Orders": "6.5"}}, "integer"),
    ({"manifest": "ema.toml", "expected": {"End Equity": True}}, "strings or numbers"),
    ({"manifest": "ema.toml", "backtest_timeout": 0}, "backtest_timeout"),
    ({"manifest": "../ema.toml"}, "outside"),
    ({"manifest": "absent.toml"}, "does not exist"),
    ({"manifest": "ema.toml", "name": "a b"}, "name"),
])
def test_invalid_check_files_are_refused_before_any_run(tmp_path, capsys, check, message):
    path = write_checks(tmp_path, [check], stems=["ema"])
    runs = ScriptedBacktests({})
    assert main([str(path), "--out", str(tmp_path / "r.json")], run_backtest=runs) == 2
    assert runs.calls == [] and not (tmp_path / "r.json").exists()
    assert message in capsys.readouterr().err


def test_duplicate_names_and_missing_project_are_refused(tmp_path, capsys):
    path = write_checks(tmp_path, [{"manifest": "ema.toml"}, {"name": "ema", "manifest": "b.toml"}],
                        stems=["ema", "b"])
    assert main([str(path), "--dry-run"], run_backtest=ScriptedBacktests({})) == 2
    assert "duplicate" in capsys.readouterr().err
    write_manifest(tmp_path, "orphan", project_id=None)
    path.write_text(json.dumps({"checks": [{"manifest": "orphan.toml"}]}))
    assert main([str(path), "--dry-run"], run_backtest=ScriptedBacktests({})) == 2
    assert "no QuantConnect project selected" in capsys.readouterr().err


def test_project_saved_by_create_is_accepted(tmp_path, capsys):
    path = write_checks(tmp_path, [{"manifest": "saved.toml"}], stems=["saved"])
    write_manifest(tmp_path, "saved", project_id=None)
    (tmp_path / "artifacts" / "saved").mkdir(parents=True)
    (tmp_path / "artifacts" / "saved" / "cloud.json").write_text('{"project_id": 99}')
    assert main([str(path), "--dry-run"], run_backtest=ScriptedBacktests({})) == 0
    assert "project 99 (artifacts/saved/cloud.json)" in capsys.readouterr().out


def test_an_existing_output_file_is_not_overwritten(tmp_path, capsys):
    path = write_checks(tmp_path, THREE[:1])
    out = tmp_path / "r.json"
    out.write_text("earlier results")
    runs = ScriptedBacktests({"ema": EMA_STATISTICS})
    assert main([str(path), "--out", str(out)], run_backtest=runs) == 2
    assert runs.calls == [] and out.read_text() == "earlier results"


def test_interruption_keeps_the_checks_already_run(tmp_path):
    path = write_checks(tmp_path, THREE[:2])
    out = tmp_path / "r.json"
    runs = ScriptedBacktests({"ema": EMA_STATISTICS, "flat": KeyboardInterrupt()})
    assert main([str(path), "--out", str(out)], run_backtest=runs) == 130
    saved = json.loads(out.read_text())
    assert [c["name"] for c in saved["checks"]] == ["ema"]


def test_the_repository_check_file_matches_the_recorded_backtests(monkeypatch):
    # Project selection lives in git-ignored artifacts, so it is not part of this test.
    monkeypatch.setattr(native_checks, "_project", lambda manifest: "selected")
    checks = native_checks.load_checks(REPO / "deployments/quantconnect/native_checks.json")
    assert [(c.name, c.manifest.name, c.recorded_backtest) for c in checks] == [
        ("ema", "ema.toml", "cc7a7dba9388a8a5b0cd70a284dcc6fc"),
        ("ema_forecasts", "ema_forecasts.toml", "84a094e67e1a467c4de51e06fcd44607"),
        ("index_check", "index_check.toml", "8e2a97ed460cdf6920f92d023b99cf9e"),
        ("warm_up_check", "warm_up_check.toml", "6509e4ac1a1b6413d63519d1f063827f"),
        ("coverage_check", "coverage_check.toml", "509b6e541c322cc055b12605afa2f78c"),
    ]
    assert native_checks.evaluate(checks[0], EMA_STATISTICS)[0] == "pass"
    assert native_checks.evaluate(checks[1], NO_ORDERS)[0] == "pass"
    assert native_checks.evaluate(checks[2], NO_ORDERS)[0] == "pass"
    assert native_checks.evaluate(checks[0], {**EMA_STATISTICS, "End Equity": "99600"})[0] \
        == "fail"


def _coverage_probe():
    from deployments.quantconnect.coverage_check import CoverageProbe

    return CoverageProbe(date(2025, 7, 21))


def test_the_coverage_report_names_every_declaration_without_an_arrival():
    from deployments.quantconnect.coverage_check import missing, summarize

    probe = _coverage_probe()
    live = datetime(2025, 7, 21, 14, 31, tzinfo=UTC)
    for name, tally in probe.tallies.items():
        if name != "spy_quotes":
            tally.add(live, warming_up=False)
    for tally in probe.indicator_tallies.values():
        tally.add(live, warming_up=False)
    # Per-instrument earnings tallies are not declarations: SPY has no listings.
    probe.tallies["earnings:USA:SPY"] = type(probe.tallies["earnings"])()
    assert missing(summarize(probe)) == ["spy_quotes"]


def test_the_coverage_log_line_is_one_json_line_of_bounded_length():
    from deployments.quantconnect.coverage_check import LOGGED_FILLS, log_line, summarize

    probe = _coverage_probe()
    fills = [{"what": f"window daily member{i}", "clock": "2025-07-11T04:00:00+00:00",
              "callback": "on_securities_changed"} for i in range(500)]
    line = log_line(summarize(probe), fills)
    assert "\n" not in line and line.startswith("COVERAGE_CHECK {")
    decoded = json.loads(line.removeprefix("COVERAGE_CHECK "))
    assert set(decoded["inputs"]) == set(probe.tallies)
    assert set(decoded["indicators"]) == set(probe.indicator_tallies)
    assert decoded["fill_count"] == 500 and len(decoded["fills"]) == LOGGED_FILLS
    assert len(line) < 8000
