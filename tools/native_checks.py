"""Run the QuantConnect native checks and compare their statistics with expected values.

Usage, from the repository root::

    uv run --cache-dir .uv-cache python tools/native_checks.py \\
        deployments/quantconnect/native_checks.json [--only NAME] [--dry-run] [--out PATH]

The check file is JSON: ``{"checks": [{...}, ...]}``. Each check has

- ``manifest`` (required): a deployment manifest, relative to the check file's folder and
  inside it. Its QuantConnect project is the manifest's ``project_id`` or the ``project_id``
  saved in ``artifacts/<manifest stem>/cloud.json`` by ``sugarloaf create``; a check whose
  manifest selects no project is refused before any backtest runs.
- ``name`` (default: the manifest's stem): letters, digits, ``.``, ``_`` and ``-``; selected
  with ``--only`` and used in the backtest's name.
- ``expected``: statistic name to value, as QuantConnect reports it (``"$6.00"``,
  ``"99665.11"``, ``"-0.335%"``) or as a number. ``Total Orders`` is compared exactly as an
  integer; a value that parses as a number (after removing ``$``, ``%`` and thousands
  separators) is compared within the tolerance; any other value is compared as text.
- ``tolerance``: statistic name to the largest absolute difference accepted, in the
  statistic's own unit (dollars, percentage points). Default zero.
- ``backtest_timeout``: seconds to wait for one backtest, default 1800, at most 21600.
- ``recorded_backtest`` and ``description``: the backtest the expectations came from, and a
  note; reported, never read from QuantConnect.

A check with no ``expected`` values is reported as ``recorded``. Each backtest runs through
``run_backtest`` (upload, compile, backtest, status, logs), one after another. The output
file holds each check's backtest id, status, comparisons and full statistics; it is
rewritten after every check, so an interrupted session keeps the checks already run.

Exit status: 0 when every check passes or is recorded, 1 when a check fails or a run
raises, 2 when the check file or the arguments are invalid (nothing has run), 130 on
interruption. Credentials are read only by the QuantConnect client; error messages are
printed and saved with the user id and API token replaced by ``[redacted]``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 1800.0
MAX_TIMEOUT = 21600.0
EXACT = "Total Orders"
CHECK_KEYS = {"name", "manifest", "expected", "tolerance", "backtest_timeout",
              "recorded_backtest", "description"}
FILE_KEYS = {"checks", "description"}
NAME = re.compile(r"[A-Za-z0-9_.-]+")
# Shown for a check with no expectations, so its line still says what the run did.
SUMMARY_FIELDS = ("Total Orders", "Total Fees", "End Equity", "Net Profit")


class CheckFileError(ValueError):
    """The check file or the arguments cannot be run as given."""


@dataclass(frozen=True)
class Check:
    """One manifest to run and the statistics expected from its backtest."""

    name: str
    manifest: Path
    expected: dict[str, object]
    tolerance: dict[str, Decimal]
    backtest_timeout: float = DEFAULT_TIMEOUT
    recorded_backtest: str | None = None
    project: str = ""
    description: str = ""


@dataclass
class Comparison:
    """One statistic: expected and observed values, and whether they agree."""

    statistic: str
    expected: object
    observed: object
    passed: bool
    detail: str = ""

    def line(self) -> str:
        """The comparison as plain ASCII text for the console."""
        mark = "ok" if self.passed else "FAIL"
        return f"{self.statistic} {self.observed} vs {self.expected} {self.detail} {mark}"


@dataclass
class Outcome:
    """What one check produced: status, backtest id, comparisons, statistics or error."""

    check: Check
    status: str
    backtest_id: str | None = None
    comparisons: list[Comparison] = field(default_factory=list)
    run: dict = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------- values


def parse_number(value: object) -> tuple[Decimal, str] | None:
    """Parse a QuantConnect statistic such as ``"$6.00"``, ``"-0.335%"`` or ``"$-334.89"``.

    Returns:
        The number and its unit (``"$"``, ``"%"`` or ``""``), or None for text.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Decimal(str(value)), ""
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace(" ", "")
    unit = ""
    if text.endswith("%"):
        unit, text = "%", text[:-1]
    sign = ""
    if text[:1] in "+-" and text[1:2] == "$":
        sign, text = text[0], text[1:]
    if text.startswith("$"):
        if unit:
            return None
        unit, text = "$", text[1:]
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", text):
        return None
    try:
        return Decimal(sign + text), unit
    except InvalidOperation:
        return None


def compare(statistic: str, expected: object, observed: object,
            tolerance: Decimal) -> Comparison:
    """Compare one statistic: exact integer for Total Orders, within tolerance for numbers."""
    if observed is None:
        return Comparison(statistic, expected, None, False, "(missing from the backtest)")
    if statistic == EXACT:
        want, got = parse_number(expected), parse_number(observed)
        ok = (want is not None and got is not None and want[0] == want[0].to_integral_value()
              and got[0] == want[0])
        return Comparison(statistic, expected, observed, ok, "(exact)")
    want, got = parse_number(expected), parse_number(observed)
    if want is None:
        return Comparison(statistic, expected, observed, str(expected) == str(observed), "(text)")
    if got is None:
        return Comparison(statistic, expected, observed, False, "(observed value is not a number)")
    if want[1] and want[1] != got[1]:
        return Comparison(statistic, expected, observed, False,
                          f"(unit {got[1] or 'none'} differs from {want[1]})")
    difference = abs(got[0] - want[0])
    return Comparison(statistic, expected, observed, difference <= tolerance,
                      f"(diff {difference} tol {tolerance})")


def evaluate(check: Check, statistics: Mapping[str, object]) -> tuple[str, list[Comparison]]:
    """Return the check's status (pass, fail or recorded) and its comparisons."""
    if not check.expected:
        return "recorded", []
    comparisons = [
        compare(name, value, statistics.get(name), check.tolerance.get(name, Decimal(0)))
        for name, value in check.expected.items()
    ]
    return ("pass" if all(c.passed for c in comparisons) else "fail"), comparisons


# ---------------------------------------------------------------- check file


def _project(manifest: Path) -> str:
    # Mirror cloud/backtests.py: the manifest's project_id, else the saved cloud.json one.
    # Both present and different is refused there too, so refuse it before any run.
    from sugarloaf.adapters.quantconnect.cloud.packaging import read_spec

    spec = read_spec(manifest)
    saved = spec.artifacts / "cloud.json"
    state = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else {}
    if spec.project_id and state.get("project_id", spec.project_id) != spec.project_id:
        raise CheckFileError(f"{manifest.name}: project_id {spec.project_id} differs from "
                             f"{saved} ({state.get('project_id')})")
    if spec.project_id:
        return f"{spec.project_id} (manifest)"
    if state.get("project_id"):
        return f"{state['project_id']} (artifacts/{manifest.stem}/cloud.json)"
    raise CheckFileError(
        f"{manifest.name}: no QuantConnect project selected; set project_id in the manifest or "
        f"run `sugarloaf create --adapter quantconnect --manifest {manifest}` once"
    )


def _check(entry: object, folder: Path, index: int) -> Check:
    where = f"checks[{index}]"
    if not isinstance(entry, dict):
        raise CheckFileError(f"{where} is not an object")
    unknown = set(entry) - CHECK_KEYS
    if unknown:
        raise CheckFileError(
            f"{where}: unknown keys {sorted(unknown)}; allowed {sorted(CHECK_KEYS)}"
        )
    manifest_text = entry.get("manifest")
    if not isinstance(manifest_text, str) or not manifest_text:
        raise CheckFileError(f"{where}: manifest is required")
    manifest = (folder / manifest_text).resolve()
    if not manifest.is_relative_to(folder.resolve()):
        raise CheckFileError(f"{where}: manifest {manifest_text} is outside {folder}")
    if not manifest.is_file():
        raise CheckFileError(f"{where}: manifest {manifest_text} does not exist")
    name = entry.get("name", manifest.stem)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CheckFileError(f"{where}: name must be letters, digits, '.', '_' or '-'")
    expected = entry.get("expected", {})
    if not isinstance(expected, dict) or not all(
        isinstance(v, str) or (isinstance(v, int | float) and not isinstance(v, bool))
        for v in expected.values()
    ):
        raise CheckFileError(f"{where}: expected maps statistic names to strings or numbers")
    if EXACT in expected:
        value = parse_number(expected[EXACT])
        if value is None or value[1] or value[0] != value[0].to_integral_value():
            raise CheckFileError(f"{where}: {EXACT} must be an integer")
    tolerance_in = entry.get("tolerance", {})
    if not isinstance(tolerance_in, dict):
        raise CheckFileError(f"{where}: tolerance maps statistic names to numbers")
    tolerance = {}
    for statistic, value in tolerance_in.items():
        if statistic not in expected:
            raise CheckFileError(f"{where}: tolerance for {statistic!r}, which has no expectation")
        if statistic == EXACT:
            raise CheckFileError(f"{where}: {EXACT} is compared exactly; remove its tolerance")
        if isinstance(value, bool) or not isinstance(value, int | float) \
                or not math.isfinite(value) or value < 0:
            raise CheckFileError(f"{where}: tolerance for {statistic!r} must be a number >= 0")
        if parse_number(expected[statistic]) is None:
            raise CheckFileError(f"{where}: {statistic!r} is text; a tolerance does not apply")
        tolerance[statistic] = Decimal(str(value))
    timeout = entry.get("backtest_timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) \
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise CheckFileError(f"{where}: backtest_timeout must be in (0, {MAX_TIMEOUT:g}] seconds")
    recorded = entry.get("recorded_backtest")
    if recorded is not None and not (isinstance(recorded, str) and NAME.fullmatch(recorded)):
        raise CheckFileError(f"{where}: recorded_backtest must be a backtest id")
    description = entry.get("description", "")
    if not isinstance(description, str):
        raise CheckFileError(f"{where}: description must be text")
    return Check(name, manifest, dict(expected), tolerance, float(timeout), recorded,
                 _project(manifest), description)


def load_checks(path: Path) -> list[Check]:
    """Read and validate a check file; raise CheckFileError naming the first problem."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CheckFileError(f"{path} does not exist") from None
    except json.JSONDecodeError as error:
        raise CheckFileError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        raise CheckFileError(f'{path} must be an object with a "checks" list')
    unknown = set(data) - FILE_KEYS
    if unknown:
        raise CheckFileError(f"{path}: unknown keys {sorted(unknown)}")
    checks = [_check(entry, path.parent, i) for i, entry in enumerate(data["checks"])]
    names = [c.name for c in checks]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise CheckFileError(f"{path}: duplicate check names {duplicates}")
    if not checks:
        raise CheckFileError(f"{path} lists no checks")
    return checks


def select(checks: list[Check], only: Sequence[str] | None) -> list[Check]:
    """Keep the named checks, in file order; an unknown name is an error."""
    if not only:
        return checks
    known = {c.name for c in checks}
    missing = sorted(set(only) - known)
    if missing:
        raise CheckFileError(f"--only {missing}: no such check; known {sorted(known)}")
    return [c for c in checks if c.name in only]


# ---------------------------------------------------------------- credentials and output


def secrets() -> tuple[list[str], list[str]]:
    """Credential values to keep out of printed and saved text: (user ids, API tokens).

    Error messages are redacted of both. Everything else is redacted of the tokens only:
    a numeric user id can also occur inside a statistic, which must stay intact.
    """
    users = [os.environ.get("QC_USER_ID", "")]
    tokens = [os.environ.get("QC_API_TOKEN", "")]
    try:
        from sugarloaf.adapters.quantconnect.cloud.client import load_credentials

        user, token = load_credentials()
        users.append(user)
        tokens.append(token)
    except Exception:  # no saved login: nothing more to redact
        pass
    return [v for v in users if v], [v for v in tokens if v]


def redact(text: str, hidden: Sequence[str]) -> str:
    """Replace every value in ``hidden`` within ``text`` with ``[redacted]``."""
    # Longest first, so a value containing another is replaced whole.
    for value in sorted(set(hidden), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def relative(path: Path) -> str:
    """A path relative to the repository when it is inside it."""
    return path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)


def console_line(outcome: Outcome) -> str:
    """One line per check: name, manifest, backtest id, status, compared values."""
    check = outcome.check
    head = (f"{check.name:<14} {relative(check.manifest):<44} "
            f"{outcome.backtest_id or '-':<32} {outcome.status:<8}")
    if outcome.error:
        return f"{head} {outcome.error}"
    if outcome.comparisons:
        return f"{head} " + "; ".join(c.line() for c in outcome.comparisons)
    statistics = outcome.run.get("statistics") or {}
    shown = [f"{k} {statistics[k]}" for k in SUMMARY_FIELDS if k in statistics]
    return f"{head} " + "; ".join(shown)


def record(outcome: Outcome) -> dict:
    """The JSON record of one check for the output file."""
    check = outcome.check
    return {
        "name": check.name,
        "manifest": relative(check.manifest),
        "project": check.project,
        "recorded_backtest": check.recorded_backtest,
        "status": outcome.status,
        "backtest_id": outcome.backtest_id,
        "comparisons": [
            {"statistic": c.statistic, "expected": c.expected, "observed": c.observed,
             "passed": c.passed, "detail": c.detail.strip("()")}
            for c in outcome.comparisons
        ],
        "error": outcome.error,
        **{key: outcome.run.get(key) for key in
           ("project_id", "compile_id", "source_hash", "parameters", "completed", "statistics")},
    }


def write_out(path: Path, document: dict) -> None:
    """Write the output file atomically: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(handle.name, path)


# ---------------------------------------------------------------- running


def run_check(check: Check, run_backtest: Callable[..., dict], stamp: str,
              hidden: Sequence[str]) -> Outcome:
    """Run one check's backtest and compare its statistics; an exception becomes an error."""
    try:
        result = run_backtest(
            check.manifest,
            name=f"Sugarloaf native check {check.name} {stamp}",
            backtest_timeout=check.backtest_timeout,
        )
    except Exception as error:  # report and continue with the next check
        return Outcome(check, "error", error=redact(f"{type(error).__name__}: {error}", hidden))
    statistics = result.get("statistics")
    if not isinstance(statistics, dict):
        return Outcome(check, "error", result.get("backtest_id"), run=result,
                       error="the backtest returned no statistics")
    status, comparisons = evaluate(check, statistics)
    return Outcome(check, status, result.get("backtest_id"), comparisons, result)


def dry_run(checks: list[Check]) -> None:
    """List what would run, without contacting QuantConnect."""
    print(f"{len(checks)} backtest(s) would run, one after another:")
    for check in checks:
        expected = "; ".join(
            f"{k} {v}" + (f" +/- {check.tolerance[k]}" if k in check.tolerance else "")
            for k, v in check.expected.items()
        ) or "none (recorded)"
        print(f"{check.name:<14} {relative(check.manifest):<44} project {check.project}; "
              f"timeout {check.backtest_timeout:g} s; expected {expected}")


def main(argv: Sequence[str] | None = None, *,
         run_backtest: Callable[..., dict] | None = None) -> int:
    """Parse arguments, run the selected checks and return the exit status."""
    parser = argparse.ArgumentParser(
        prog="native_checks",
        description="Run QuantConnect native checks and compare their statistics.",
    )
    parser.add_argument("checks", type=Path, help="JSON check file")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="run only this check (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would run without contacting QuantConnect")
    parser.add_argument("--out", type=Path, help="output JSON (default: "
                        "artifacts/native_checks/<UTC time>.json); an existing file is refused")
    args = parser.parse_args(argv)
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    try:
        checks = select(load_checks(args.checks), args.only)
        out = (args.out or REPO / "artifacts" / "native_checks" / f"{stamp}.json").resolve()
        if not args.dry_run and out.exists():
            raise CheckFileError(f"--out {out} exists; choose a new file to keep earlier results")
    except (CheckFileError, ValueError, KeyError, OSError) as error:
        print(f"invalid: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        dry_run(checks)
        return 0

    if run_backtest is None:
        from sugarloaf.adapters.quantconnect.cloud.backtests import run_backtest
    users, tokens = secrets()
    document = {
        "check_file": relative(args.checks.resolve()),
        "check_file_sha256": hashlib.sha256(args.checks.read_bytes()).hexdigest(),
        "started_utc": started.isoformat(),
        "finished_utc": None,
        "checks": [],
    }
    outcomes: list[Outcome] = []
    status = 0
    try:
        for number, check in enumerate(checks, 1):
            print(f"[{number}/{len(checks)}] running {check.name} ({relative(check.manifest)}, "
                  f"project {check.project})", file=sys.stderr, flush=True)
            outcome = run_check(check, run_backtest, stamp, users + tokens)
            outcomes.append(outcome)
            print(redact(console_line(outcome), tokens), flush=True)
            document["checks"].append(json.loads(redact(json.dumps(record(outcome)), tokens)))
            write_out(out, document)
    except KeyboardInterrupt:
        print("interrupted; checks already run are saved", file=sys.stderr)
        status = 130
    document["finished_utc"] = datetime.now(UTC).isoformat()
    write_out(out, document)
    statuses = ("pass", "fail", "recorded", "error")
    counts = {s: sum(o.status == s for o in outcomes) for s in statuses}
    not_run = len(checks) - len(outcomes)
    print(f"summary: {counts['pass']} pass, {counts['fail']} fail, {counts['recorded']} recorded, "
          f"{counts['error']} error" + (f", {not_run} not run" if not_run else "")
          + f"; results in {relative(out)}")
    if status:
        return status
    return 1 if counts["fail"] or counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
