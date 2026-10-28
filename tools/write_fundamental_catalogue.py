"""Write the QC adapter's fundamental-field catalogue from tools/fundamental_catalogue_qc.py output.

    python tools/write_fundamental_catalogue.py PATH_TO_SUMMARY_JSON

Writes the package sugarloaf/adapters/quantconnect/fundamental_catalogue/: ``__init__.py``
with the lookup, and ``part_N.py`` files holding the field table in pieces under
QuantConnect's limit of 64,000 characters per project file. Names are ``morningstar.``
followed by the Python attribute path LEAN exposes. Each multi-period field lists its
periods; LEAN's ``value`` (its default period) and ``has_value`` are left out, so a reader
always names the period it wants.
"""

import json
import sys
from pathlib import Path

TARGET = (Path(__file__).resolve().parents[1]
          / "sugarloaf/adapters/quantconnect/fundamental_catalogue")
NOT_PERIODS = {"value", "has_value"}
PART_CHARS = 60_000
NL = "\n"


def main(path):
    summary = json.loads(Path(path).read_text(encoding="utf-8"))
    version = summary["lean_version"]
    period_sets, fields = [], {}
    for entry in summary["fields"].values():
        name = ".".join(["morningstar", *entry["path"]])
        periods = None
        if entry["periods"]:
            periods = tuple(p[1] for p in entry["periods"] if p[1] not in NOT_PERIODS)
            if periods not in period_sets:
                period_sets.append(periods)
            periods = period_sets.index(periods)
        fields[name] = (entry["type"], periods)
    # Split the table into parts that each stay well under QC's file limit.
    parts, chunk, size = [], [], 0
    for name, value in sorted(fields.items()):
        row = f"    {name!r}: {value!r},"
        if chunk and size + len(row) > PART_CHARS:
            parts.append(chunk)
            chunk, size = [], 0
        chunk.append(row)
        size += len(row) + 1
    parts.append(chunk)
    init = [
        '"""Supported US equity fundamental fields of the QC adapter. Generated; do not edit.',
        "",
        f"Made by tools/write_fundamental_catalogue.py from LEAN {version}'s Fundamental",
        "classes (tools/fundamental_catalogue_qc.py, run on a QuantConnect research kernel). A",
        "name is ``morningstar.`` and the Python attribute path; a multi-period field's value",
        "is read as ``name.period``. Types: float, int, bool, str, date. Units are as",
        "Morningstar reports them; the catalogue does not state them.",
        '"""',
        "",
        "from . import " + ", ".join(f"part_{i + 1}" for i in range(len(parts))),
        "",
        f'LEAN_VERSION = "{version}"',
        f'VERSION = "lean-{version}"',
        "",
        "# Period sets, referred to by index in FIELDS.",
        "PERIODS = (",
        *[f"    {p!r}," for p in period_sets],
        ")",
        "",
        "# name: (type, index into PERIODS, or None for a single value), from the parts.",
        "FIELDS = {}",
        *[f"FIELDS.update(part_{i + 1}.FIELDS)" for i in range(len(parts))],
        "",
        "",
        "def resolve(name):",
        '    """(attribute path, type) for a readable name, or None when it is not supported."""',
        "    entry = FIELDS.get(name)",
        "    if entry is not None:",
        "        # A multi-period field must be read with its period named.",
        '        return (tuple(name.split(".")[1:]), entry[0]) if entry[1] is None else None',
        '    base, _, period = name.rpartition(".")',
        "    entry = FIELDS.get(base)",
        "    if entry is None or entry[1] is None or period not in PERIODS[entry[1]]:",
        "        return None",
        '    return (tuple(name.split(".")[1:]), entry[0])',
        "",
    ]
    TARGET.mkdir(exist_ok=True)
    for old in TARGET.glob("part_*.py"):
        old.unlink()
    for i, rows in enumerate(parts):
        body = ['"""Part of the generated fundamental-field catalogue. Do not edit."""', "",
                "# ruff: noqa: E501", "", "FIELDS = {", *rows, "}", ""]
        (TARGET / f"part_{i + 1}.py").write_text(NL.join(body), encoding="utf-8", newline=NL)
    (TARGET / "__init__.py").write_text(NL.join(init), encoding="utf-8", newline=NL)
    sizes = [len(f.read_text(encoding="utf-8")) for f in sorted(TARGET.glob("*.py"))]
    print("fields", len(fields), "period sets", len(period_sets), "files", len(sizes),
          "largest", max(sizes))


if __name__ == "__main__":
    main(sys.argv[1])
