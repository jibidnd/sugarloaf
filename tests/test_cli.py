"""Adapter selection must stay independent of manifest formats and native clients."""

import json
from types import SimpleNamespace

import pytest

from sugarloaf import cli


def test_dispatch_preserves_adapter_arguments_and_external_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "other checkout" / "strategy.custom"
    arguments = ["run", "--manifest", str(manifest), "--parameter", "budget", "0.5"]
    observed = []

    def adapter_main(argv, *, prog):
        observed.append((argv, prog))
        return 7

    monkeypatch.setattr(cli, "_ADAPTER_COMMANDS", {"example": "example.commands"})

    def import_module(name):
        assert name == "example.commands"
        return SimpleNamespace(main=adapter_main)

    monkeypatch.setattr(cli, "import_module", import_module)
    assert cli.main([*arguments, "--adapter", "example"]) == 7
    assert observed == [(arguments, "sugarloaf --adapter example")]
    assert not manifest.exists()  # The shared entrypoint never opens/parses it.


@pytest.mark.parametrize("arguments", [
    ["run", "--manifest", "strategy.toml"],
    ["run", "--adapter", "unknown", "--manifest", "strategy.toml"],
])
def test_invalid_adapter_selection_fails_before_import(monkeypatch, arguments):
    monkeypatch.setattr(cli, "import_module", lambda _: pytest.fail("Unexpected adapter import"))
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


def test_general_help_does_not_load_adapter(monkeypatch, capsys):
    monkeypatch.setattr(cli, "import_module", lambda _: pytest.fail("Unexpected adapter import"))
    assert cli.main(["--help"]) == 0
    assert "--adapter" in capsys.readouterr().out


def test_selected_adapter_owns_help(monkeypatch, capsys):
    def adapter_main(argv, *, prog):
        assert argv == ["run", "--help"]
        print("Adapter-specific options")
        return 0

    monkeypatch.setattr(cli, "import_module", lambda _: SimpleNamespace(main=adapter_main))
    assert cli.main(["run", "--adapter", "quantconnect", "--help"]) == 0
    assert "Adapter-specific options" in capsys.readouterr().out


def test_quantconnect_packages_external_relative_manifest(monkeypatch, tmp_path, capsys):
    library_cwd = tmp_path / "library"
    library_cwd.mkdir()
    lab = tmp_path / "lab"
    manifests = lab / "deployments" / "quantconnect"
    manifests.mkdir(parents=True)
    (lab / "reference.py").write_text("# Native source\n")
    (manifests / "trend.toml").write_text(
        'name="external"\nroot="../.."\nentrypoint="reference.py"\n'
    )
    monkeypatch.chdir(library_cwd)
    assert cli.main([
        "package", "--adapter", "quantconnect", "--manifest",
        "../lab/deployments/quantconnect/trend.toml",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["files"] == 1
    bundle = json.loads((lab / "artifacts/trend/bundle.json").read_text())
    assert bundle["files"] == [{"name": "main.py", "content": "# Native source\n"}]
