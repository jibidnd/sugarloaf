"""Explicit source bundles for external strategy packages and QC deployment."""

import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import sugarloaf
from sugarloaf import __version__


@dataclass(frozen=True)
class BundleSpec:
    """Explicit source-selection manifest with either a deployment factory or native entrypoint."""
    path: Path
    root: Path
    name: str
    include: tuple[str, ...]
    factory: str | None
    entrypoint: str | None
    parameters: dict[str, str]
    project_id: int | None

    @property
    def artifacts(self) -> Path:
        """Return the local artifact directory derived from the manifest filename."""
        # Keep state with the declared source checkout, independent of the caller cwd.
        # The TOML filename stem scopes evidence; use distinct stems for distinct run configs.
        return self.root / "artifacts" / self.path.stem


def read_spec(path: Path) -> BundleSpec:
    """Read and resolve a TOML deployment manifest.

    Args:
        path: Manifest path; its root is resolved relative to the manifest directory.

    Returns:
        Explicit bundle specification with platform parameters converted to strings.

    Raises:
        ValueError: Factory/entrypoint selection or module:function syntax is invalid.
    """
    # Reading only resolves selection/configuration. It deliberately does not import
    # or call the private graph factory: that happens inside the provider runtime.
    path = path.resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    # Factory mode hosts a Sugarloaf graph; entrypoint mode uploads a native study.
    # These are alternative entry paths, so exactly one must be selected.
    factory, entrypoint = data.get("factory"), data.get("entrypoint")
    if bool(factory) == bool(entrypoint):
        raise ValueError("Specify exactly one factory or native entrypoint")
    if factory and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("Factory must be a module:function reference")
    # Manifest-relative paths make the same config work when invoked from Lab,
    # Sugarloaf, or another working directory.
    root = (path.parent / data.get("root", ".")).resolve()
    return BundleSpec(
        path,
        root,
        data["name"],
        tuple(data.get("include", ())),
        factory,
        entrypoint,
        {key: str(value) for key, value in data.get("parameters", {}).items()},
        data.get("project_id"),
    )


def _inside(root: Path, name: str) -> Path:
    # Resolve before checking containment so ../ components and symlinks cannot
    # quietly include private files outside the explicitly declared source root.
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Bundle source escapes its declared root: {name}")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _revision(root: Path) -> str | None:
    # Git identity is supplementary provenance. Uploaded byte hashes below still
    # identify local edits and working copies for which no commit is available.
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def build_bundle(spec: BundleSpec) -> dict:
    """Build an offline QC source bundle without uploading or executing it.

    Args:
        spec: Manifest selecting a factory/native entrypoint and strategy dependencies.

    Returns:
        Transformed files, content hashes, manifest text, parameters, and source revisions.

    Raises:
        ValueError: Selection escapes its root, shadows infrastructure, has an
            unsupported suffix, omits the factory, or creates a cloud filename collision.
        FileNotFoundError: An explicitly selected source does not exist.

    Directories include Python only; notebooks require explicit selection. The
    cloud-only types.py rename and import rewrite leave local source unchanged.
    Hashes identify actual contents, including uncommitted edits.
    """
    # Locate the actual installed Sugarloaf checkout, not a fixed sibling directory.
    # A bundle is a file list plus provenance; it is not an executed Deployment.
    library = Path(sugarloaf.__file__).resolve().parent
    sources: dict[str, str] = {}
    # Only factory mode supplies the reusable runtime automatically. Native studies
    # can remain ordinary provider algorithms with their explicitly selected imports.
    if spec.factory:
        for path in sorted(library.rglob("*.py")):
            name = "sugarloaf/" + path.relative_to(library).as_posix()
            if (
                name.startswith("sugarloaf/deployment/")
                or name.startswith("sugarloaf/adapters/quantconnect/cloud/")
                or name == "sugarloaf/cli.py"
                or name.endswith("quantconnect/main.py")
            ):
                continue  # Local tooling is not needed inside LEAN.
            sources[name] = path.read_text(encoding="utf-8")
        for name in ("__init__.py", "config.py", "reporting.py"):
            sources["sugarloaf/deployment/" + name] = (library / "deployment" / name).read_text(
                encoding="utf-8"
            )

    # This allowlist is the private-code boundary: include the selected strategy and
    # its dependencies, not the entire proprietary Lab checkout.
    for name in spec.include:
        selected = _inside(spec.root, name)
        paths = sorted(selected.rglob("*.py")) if selected.is_dir() else [selected]
        for path in paths:
            path = _inside(spec.root, path.relative_to(spec.root).as_posix())
            relative = path.relative_to(spec.root).as_posix()
            if path.suffix not in {".py", ".ipynb"}:
                raise ValueError(f"Only Python/notebook sources may be uploaded: {relative}")
            if relative.startswith("sugarloaf/") or relative in {"main.py", "deployment_config.py"}:
                raise ValueError(f"Strategy source shadows infrastructure: {relative}")
            sources[relative] = path.read_text(encoding="utf-8")
            # Preserve real package initializers; do not silently replace their behavior.
            for parent in path.relative_to(spec.root).parents:
                if parent == Path("."):
                    continue
                init = parent.as_posix() + "/__init__.py"
                if (spec.root / init).exists():
                    sources[init] = _inside(spec.root, init).read_text(encoding="utf-8")

    if spec.factory:
        # Generate a tiny import alias so the stable native entrypoint can load any
        # selected factory without hard-coded study imports in Sugarloaf.
        module, function = spec.factory.split(":")
        if module.replace(".", "/") + ".py" not in sources:
            raise ValueError("Factory module must be explicitly included in the bundle")
        sources["deployment_config.py"] = f"from {module} import {function} as load_deployment\n"
        # The algorithm entrypoint is copied from the maintained adapter template.
        # Only deployment_config.py is generated from the factory reference.
        sources["main.py"] = (library / "adapters/quantconnect/main.py").read_text(encoding="utf-8")
    else:
        sources["main.py"] = _inside(spec.root, spec.entrypoint).read_text(encoding="utf-8")

    # Missing ancestor initializers get empty package markers. Real initializers
    # selected above retain their code, so keep private package imports narrowly scoped.
    for name in tuple(sources):
        for parent in Path(name).parents:
            if parent != Path("."):
                sources.setdefault(parent.as_posix() + "/__init__.py", "")
    # QC disallows the basename types.py; keep this transformation at deployment.
    transformed = {}
    for name, content in sources.items():
        mapped = name.replace("sugarloaf/core/types.py", "sugarloaf/core/primitives.py")
        if mapped in transformed:
            raise ValueError(f"Cloud filename collision: {mapped}")
        transformed[mapped] = content.replace("sugarloaf.core.types", "sugarloaf.core.primitives")
    # Hash the transformed cloud bytes because they are the exact submitted files;
    # the untransformed local source is not the provider-visible bundle.
    manifest = {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in sorted(transformed.items())
    }
    return {
        "files": [{"name": name, "content": transformed[name]} for name in sorted(transformed)],
        "source_manifest": manifest,
        "source_hash": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "manifest_hash": hashlib.sha256(spec.path.read_bytes()).hexdigest(),
        "manifest": spec.path.read_text(encoding="utf-8"),
        "parameters": spec.parameters,
        "factory": spec.factory,
        "entrypoint": spec.entrypoint,
        "sugarloaf_version": __version__,
        "sugarloaf_revision": _revision(library.parent),
        "strategy_revision": _revision(spec.root),
    }
