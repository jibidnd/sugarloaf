"""Build repository-relative documentation without copying authored pages."""

from pathlib import Path

from mkdocs.structure.files import File, Files, InclusionLevel


def on_files(files, config):
    """Publish guides and explicitly selected public source files at repo paths."""
    root = Path(config.config_file_path).parent
    result = []
    docs_root = Path(config.docs_dir).resolve()
    for file in files:
        if file.abs_src_path and Path(file.abs_src_path).is_relative_to(docs_root):
            result.append(File.generated(
                config, f"docs/{file.src_uri}", abs_src_path=file.abs_src_path,
                inclusion=InclusionLevel.NOT_IN_NAV,
            ))
        else:
            result.append(file)
    for folder in ("sugarloaf", "examples", "deployments", "tests"):
        for path in sorted((root / folder).rglob("*")):
            if path.suffix not in {".md", ".py", ".toml", ".ipynb"}:
                continue
            if "__pycache__" in path.parts:
                continue
            result.append(File.generated(
                config, path.relative_to(root).as_posix(), abs_src_path=str(path),
                inclusion=InclusionLevel.NOT_IN_NAV,
            ))
    for route in ("index.md", "README.md"):
        result.append(File.generated(
            config, route, abs_src_path=str(root / "README.md"),
            inclusion=InclusionLevel.NOT_IN_NAV,
        ))
    result.append(File.generated(
        config, "pyproject.toml", abs_src_path=str(root / "pyproject.toml"),
        inclusion=InclusionLevel.NOT_IN_NAV,
    ))
    return Files(result)
