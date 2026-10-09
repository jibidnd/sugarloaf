"""Deployment isolation: explicit sources, external factories, stable source identity."""

from pathlib import Path

import pytest

from sugarloaf.adapters.quantconnect.cloud.packaging import build_bundle, read_spec


def test_external_strategy_bundles_only_selected_sources(tmp_path):
    package = tmp_path / "private_strategy"
    package.mkdir()
    (package / "__init__.py").write_text("VERSION = 1\n")
    selected = package / "strategy.py"
    selected.write_text("def deployment(parameter):\n    return None\n")
    (package / "unrelated_alpha.py").write_text("SECRET = 42\n")
    (package / "research.ipynb").write_text("{}")
    manifest = tmp_path / "strategy.toml"
    manifest.write_text(
        'name = "test"\nfactory = "private_strategy.strategy:deployment"\n'
        'include = ["private_strategy/strategy.py"]\n'
    )
    spec = read_spec(manifest)
    bundle = build_bundle(spec)
    files = {file["name"]: file["content"] for file in bundle["files"]}
    assert "private_strategy/unrelated_alpha.py" not in files
    assert "private_strategy/research.ipynb" not in files
    assert files["private_strategy/__init__.py"] == "VERSION = 1\n"
    assert "private_strategy.strategy import deployment" in files["deployment_config.py"]
    assert "sugarloaf/core/primitives.py" in files
    assert "sugarloaf/adapters/quantconnect/cloud/backtests.py" not in files
    assert "sugarloaf/cli.py" not in files
    assert not any(name.startswith("sugarloaf/adapters/quantconnect/cloud/") for name in files)
    assert list(files).count("main.py") == 1
    assert build_bundle(spec)["source_hash"] == bundle["source_hash"]
    selected.write_text("def deployment(parameter):\n    return 'changed'\n")
    assert build_bundle(spec)["source_hash"] != bundle["source_hash"]


def test_manifest_rejects_path_escape_missing_factory_and_source_shadowing(tmp_path):
    manifest = tmp_path / "strategy.toml"
    prefix = 'name = "test"\nfactory = "strategy:deployment"\n'
    manifest.write_text(prefix + 'include = ["../outside.py"]\n')
    with pytest.raises(ValueError, match="escapes"):
        build_bundle(read_spec(manifest))
    manifest.write_text(prefix + "include = []\n")
    with pytest.raises(ValueError, match="Factory module"):
        build_bundle(read_spec(manifest))
    (tmp_path / "deployment_config.py").write_text("pass\n")
    manifest.write_text(prefix + 'include = ["deployment_config.py"]\n')
    with pytest.raises(ValueError, match="shadows"):
        build_bundle(read_spec(manifest))


def test_native_reference_is_separate_from_framework_and_other_studies(tmp_path):
    (tmp_path / "reference.py").write_text("# Native algorithm\n")
    manifest = tmp_path / "reference.toml"
    manifest.write_text('name = "reference"\nentrypoint = "reference.py"\n')
    bundle = build_bundle(read_spec(manifest))
    assert bundle["files"] == [{"name": "main.py", "content": "# Native algorithm\n"}]


def test_public_ema_bundle_has_no_private_strategy_dependency():
    root = Path(__file__).resolve().parents[1]
    bundle = build_bundle(read_spec(root / "deployments/quantconnect/ema.toml"))
    for file in bundle["files"]:
        assert "sugarloaf_lab" not in file["content"]
        assert "merton" not in file["name"]
        if file["name"].endswith(".py"):
            compile(file["content"], file["name"], "exec")
