---
name: sugarloaf-qc-validation
description: Reproduce and validate Sugarloaf native QuantConnect/LEAN integration behavior with exact source and configuration provenance. Use for adapter or deployment integration checks and platform quirks; exclude alpha searches, strategy optimization, and live deployment.
---

# Sugarloaf QC Validation

Answer one bounded integration question: what should happen, what actually happens,
and which source/environment that evidence covers. Keep the task distinct from
economic validation. The skill does not authorize additional cloud mutations,
paid searches, release, or live trading.

## Frame the check

Read the affected implementation and [adapter contract](../../../docs/technical/ADAPTERS.md),
[existing validation](../../../docs/VALIDATION.md), and the relevant portion of
[the guide](../../../docs/GUIDE.md). For bundling, use
[external packages](../../../docs/EXTERNAL_STRATEGIES.md) and the actual manifest.

State the expected observable behavior, minimal reproducer, acceptance criterion,
and existing evidence. Use a deterministic local test where it answers the
question; supplied API boundaries cannot establish native Python.NET overloads or
LEAN callback behavior. Check official QC/LEAN documentation and relevant
version-matched source for consequential platform assumptions. Record the versions
actually observed; do not substitute current docs for evidence about an older run.

## Preserve source and action scope

Reuse the selected manifest, factory, and established QC project where appropriate.
Resolve tool availability and authentication separately: an advertised tool is not
proof that an authenticated project operation works. Inspect the actual tool
schema and follow its project-discovery requirements before project operations.

Prefer Sugarloaf's existing packaging workflow over ad hoc cloud edits. From the
appropriate checkout, build the chosen bundle offline:

```powershell
sugarloaf package --adapter quantconnect --manifest PATH_TO_MANIFEST
```

Inspect the selected files, actual source hashes, parameters, and artifact location
before uploading. Include only the selected implementation's dependencies; keep
credentials and private strategy code out of public examples. Account for local
edits and the actual Sugarloaf source, not just a Git revision. If cloud edits are
necessary, reconcile them into local source and retain the executed snapshot.

Confirm that project mutation and the proposed compute are within the user's
authorized scope. Agree on a budget and stopping rule before substantial paid
compute; do not start an optimization or broaden the question. Keep concurrent
checks in separate projects and reuse a check's project for successive runs.
Do not overwrite unrelated cloud files or delete completed evidence.

## Observe completion

Within that scope, follow the existing upload, compile, compile-status, backtest,
status, and logs workflow. Upload changes the source snapshot, so an older compile
or run cannot validate it. Require successful compilation of the intended snapshot
before a backtest; compilation alone does not establish runtime behavior.

Inspect a completed run's status, errors, relevant logs, and the outputs that answer
the integration question, such as native readiness, admitted batch timing, orders,
fills, or cancellations. Use the completion/retrieval mechanism actually supported
by the tool/client; avoid redundant polling and do not assume a pending run passed.
If a check fails, retain its evidence and make only corrections within scope. Stop
when the question is resolved, the agreed budget is exhausted, or further progress
requires a design/scope decision. Report partial verification candidly.

## Retain evidence

Use the established per-manifest artifact paths. Retain the executed bundle/source
hashes, parameters, available revisions and local edits, project/compile/run IDs,
environment versions, relevant data/date assumptions, logs/results, and failures.
Do not fabricate missing provenance or treat an ignored artifact as backed up.

Update the affected adapter documentation or [validation record](../../../docs/VALIDATION.md)
with expected versus observed behavior, reproducible evidence, version, workaround
if needed, and verification limits. Label the finding as documented, observed,
reproduced, or provisional as appropriate. Link a concise reusable vault explanation
to repo evidence under the existing knowledge-capture policy. A Lab discovery can
motivate a Sugarloaf change but does not itself authorize that change.

Finish with the answer to the integration question and its scope. Distinguish local
correctness, native runtime compatibility, and economic validity. Historical
backtests do not establish live restart/recovery support or authorize deployment.
