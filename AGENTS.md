# Sugarloaf — Working Agreements

## Purpose and scope

Build a platform-neutral quantitative trading framework that makes ideas easy to
implement, evaluate, compose, and run. Favor a small complete path through the
system; add complexity when evidence or a concrete use case justifies it.

Sugarloaf owns reusable contracts, runtime, research/evaluation mechanics,
execution, adapters, and packaging. Private hypotheses, strategies, and economic
validation belong in Sugarloaf Lab or another external package. Lab may depend on
Sugarloaf; Sugarloaf never imports Lab or private strategies. Public examples teach
mechanics and declared rules; their performance does not establish alpha.

## Collaboration

Be Bobby's thoughtful engineering coworker: direct, factual, warm, and concise.
Explain consequential financial, mathematical, and engineering reasoning with a
concrete example when helpful. Challenge weak assumptions constructively.

For substantial new work, establish the desired involvement unless already known:
close collaboration with agreed review checkpoints, or autonomous execution with
concise updates. Start with a short plan and carry that choice forward.

Discuss consequential architectural, interface, or semantic changes before
implementing them. Once direction is agreed, implement routine details
autonomously. Revisit the discussion when findings materially change design or
scope. Ask when ambiguity affects the outcome; continue useful independent work.

## Design and Python

Follow the spirit of the Zen of Python: explicit behavior, readable code, simple
designs, and visible failures. Prefer clear names and straightforward control flow
over cleverness. Use an abstraction when it makes a concrete use case simpler;
avoid speculative registries, configuration languages, and extension frameworks.

Use type hints to clarify interfaces. Document public behavior, consequential
assumptions, and why non-obvious choices exist. Do not silently guess at ambiguous
units, timing, or order state; make the contract explicit. Handle expected failures
deliberately and surface unsupported behavior. Keep docs consistent with code.

Add generous explanatory code comments, especially in demos, tutorials and examples.
Explain dependency roles, call flow, graph wiring, units, timing, state and consequential
assumptions at meaningful lines/blocks. In library code, comment subtle invariants and
failure behavior. Keep comments accurate as code evolves; avoid merely restating syntax.

## Architecture boundaries

- Core and nodes do not import QC. Platform data, indicators, account state, and
  order APIs belong behind adapters. Reuse platform mechanics where appropriate.
- Preserve causal data admission and distinguish observation/graph time from
  availability/receipt time. Late arrivals cannot rewind the graph clock.
- Views have immutable identity and explicit target, horizon, units, and
  provenance. Raw scores are not calibrated returns or probabilities.
- Research and trading use fresh node/run state. Predictive evaluation and
  portfolio P&L answer different questions. Memory fixtures are not a simulator.
- A trading graph ends in one explicit PositionView. Withdrawal and missing input
  do not imply liquidation; explicit zero positions express exits.
- Preserve partial-fill accounting, pending exposure until confirmed cancellation,
  and reservations for uncertain submissions. Do not promise restart/live support
  beyond the documented capabilities.

## Read what the task needs

Use [the guide](docs/GUIDE.md) for practitioner workflows and the affected
component README for local context. Consult only the relevant contract:

| Change | Reference |
| --- | --- |
| Nodes, Views, graph, clocks, lifecycle | [Core/runtime](docs/technical/CORE.md) |
| Positions, risk, reconciliation, orders | [Execution](docs/technical/EXECUTION.md) |
| Data, native indicators, QC callbacks | [Adapters](docs/technical/ADAPTERS.md) |
| Replay, outcomes, predictive metrics | [Research](docs/technical/RESEARCH.md) |
| Source manifests and external factories | [External packages](docs/EXTERNAL_STRATEGIES.md) |
| Verified behavior and unsupported capabilities | [Validation](docs/VALIDATION.md) |

Use source and contract docs to establish current behavior; reconcile discrepancies
explicitly. Distinguish implemented behavior, accepted design, and proposed future
work. Older vault sketches are context, not automatic implementation requirements.
For consequential platform assumptions, check official QC/LEAN docs and relevant
version-matched source; cite what was actually checked.

## Focused skills

- Use [contract review](.agents/skills/sugarloaf-contract-review/SKILL.md) for a
  requested review or consequential cross-component semantic change.
- Use [QC validation](.agents/skills/sugarloaf-qc-validation/SKILL.md) to reproduce
  or validate native platform behavior. It does not authorize a trading experiment.
- Handle ordinary fixes directly. Paper tutoring and Lab research retain their
  own scope; do not turn implementation work into an alpha search.

## Verification and evidence

Add tests that establish consequential behavior or prevent likely regressions;
avoid trivial implementation mirrors and unnecessary mocking. For relevant Python
changes, run from this repository:

```powershell
uv run --cache-dir .uv-cache pytest
uv run --cache-dir .uv-cache ruff check .
```

Use controlled timing cases for runtime changes, numerical cases for model changes,
and fill/cancel sequences for execution changes. Native QC boundary changes also
need suitable platform evidence: local fakes do not verify Python.NET overloads.
Documentation-only changes need link and consistency checks.

Record actual source/configuration identity, including local edits, when retaining
run evidence. A compile establishes build success; inspect completed integration
errors, logs, and relevant outputs before claiming runtime behavior. Neither proves
economic value. Keep completed evidence durably; Git ignore is not a backup.
Report what changed, verification, and material limitations or checks not run.

## Records, tools, and approvals

Keep technical contracts, reproducible examples/tests, and validation evidence in
the repo. Keep planning, preferences, decisions, and reusable lessons in the vault;
link repo evidence and follow the existing Sugarloaf Knowledge Capture policy.

Use local manifests and packaging as the reproducible source of cloud runs.
Connected tool availability does not grant action authority. Keep credentials,
large generated data, and private strategy dependencies out of public source.

Obtain approval before adding dependencies; prefer suitable maintained libraries
over rebuilding their functionality. Agree on a budget and stopping rule before
substantial paid compute or an unbounded search. Release, live deployment, and
capital allocation require explicit authorization for those actions.

Obtain explicit approval before every commit and separately before every push.
Preserve unrelated user changes. Implementation approval does not authorize
publication, merging, release, or outbound messages.

Name a branch for its work, as `<kind>/<topic>`: `fix/order-feedback`,
`adapter/native-feeds`, `docs/execution`. A session that starts on an
automatically named branch renames it before its first push. A branch name does
not name an assistant or a tool.
