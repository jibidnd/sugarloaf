"""Gather one review backtest's evidence into a committed result file.

The run tooling keeps a backtest's status and logs under the manifest's git-ignored
``artifacts/<stem>/`` folder. This script reads them and writes
``tools/qc_adapter_review/results/<stem>-<backtest id>.json`` with the run identity (project,
backtest, node, LEAN version, source hash), the platform statistics, and every diagnostic
record the run logged: ``NATIVE_DIAG`` lines from the native scripts, ``SUGARLOAF_RUN`` /
``SUGARLOAF_RESULT`` and the report records from a Sugarloaf deployment.

    uv run --cache-dir .uv-cache python tools/qc_adapter_review/collect.py STEM BACKTEST_ID
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def parse_records(logs):
    """Diagnostic JSON records from the log lines, with their algorithm time stamps."""
    records = []
    for line in logs:
        stamp = STAMP.match(line)
        at = line[:19] if stamp else None
        body = line[20:] if stamp else line
        label, _, rest = body.partition(" ")
        if label in ("NATIVE_DIAG", "SUGARLOAF_RUN", "SUGARLOAF_RESULT", "SUGARLOAF_MEMBERS") or (
                label.startswith("probe.") or label.startswith("move_size")):
            try:
                value = json.loads(rest)
            except json.JSONDecodeError:
                value = rest
            records.append({"at": at, "label": label, "value": value})
    return records


def main(stem, backtest_id):
    artifacts = ROOT / "artifacts" / stem
    status = json.loads((artifacts / f"{backtest_id}.json").read_text(encoding="utf-8"))
    deployment = status["deployment"]
    backtest = status["backtest"]
    logs = json.loads((artifacts / f"{backtest_id}-logs.json").read_text(encoding="utf-8"))["logs"]
    lean = next((re.search(r"LEAN Engine (v[\d.]+)", line).group(1) for line in logs
                 if "LEAN Engine v" in line), None)
    result = {
        "manifest": stem,
        "project_id": deployment["project_id"],
        "backtest_id": backtest_id,
        "name": backtest.get("name"),
        "node": backtest.get("nodeName"),
        "lean": lean,
        "created": backtest.get("created"),
        "status": backtest.get("status"),
        "error": backtest.get("error"),
        "backtest_start": backtest.get("backtestStart"),
        "backtest_end": backtest.get("backtestEnd"),
        "source_hash": deployment.get("source_hash"),
        "sugarloaf_revision": deployment.get("sugarloaf_revision"),
        "parameters": deployment.get("parameters"),
        "runtime_statistics": backtest.get("runtimeStatistics"),
        "statistics": backtest.get("statistics"),
        "log_lines": len(logs),
        "records": parse_records(logs),
        # Lines that are not diagnostic records: platform messages, warnings and errors.
        "other_log_lines": [line for line in logs if not any(
            tag in line for tag in ("NATIVE_DIAG", "SUGARLOAF_", " probe.", " move_size"))][:60],
    }
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{stem}-{backtest_id}.json"
    path.write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
