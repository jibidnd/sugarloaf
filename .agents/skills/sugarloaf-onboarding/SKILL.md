---
name: sugarloaf-onboarding
description: Onboard an engineer or quant to use Sugarloaf as a standalone tool and maintain its code. Guide installation and usage, strategy composition, Python implementations, events and clocks, adapters, research evaluation, orders, and deployment. Use for onboarding or a guided tutorial; ordinary fixes, reviews, and strategy searches do not require this tutorial.
---

# Sugarloaf Onboarding

Teach the learner to use Sugarloaf independently: understand the environment,
use public examples, implement a node, compose a graph, research predictions,
configure execution, package a deployment, and maintain the code. Connect every
workflow to how it works and where it is implemented. Use the numbered
[syllabus](references/syllabus.md).
The default is a compact guided tutorial, approximately two to three hours of
core explanation depending on questions, with no assignments, exercises, quizzes,
or required reading outside the conversation.

## Establish the starting point

- Follow an explicit requested section or existing tutorial position. Otherwise
  start at 1.1; do not make the learner repeat their background or approve a plan
  when the conversation already establishes it.
- Orient the learner briefly: Sugarloaf owns reusable contracts, graph runtime,
  adapters, research/evaluation, execution, and packaging. Strategy packages
  depend on it; the framework does not import private strategies. QuantConnect
  supplies the first platform adapter.
- Treat Sugarloaf as a standalone tool. Its own checkout and public examples are
  sufficient for onboarding; a second repository is not required. Suggest a
  separate strategy repository only as optional organization or isolation for
  the learner's own strategies. Show ordinary public imports and Python factories,
  not an assumed companion checkout or a private strategy registry.
  Keep teaching scoped to Sugarloaf and its public examples; do not introduce
  named companion projects or use their implementations as teaching examples.
- Use the current checkout. The repository root is three directories above this
  skill folder. Read [the guide](../../../docs/GUIDE.md) and
  [validation](../../../docs/VALIDATION.md) for orientation, then read only the
  current subsection's relevant contracts, implementation, callers, and tests.
- The syllabus is a teaching map, not a frozen API specification. Verify current
  class names, methods, callback order, and capabilities before explaining them.
  Resolve moved paths through repository search rather than inventing behavior.
  If source, docs, and recorded evidence disagree, explain the discrepancy and
  what each actually establishes; do not silently repair the repository.

## Teach one subsection at a time

1. State its number and title, then explain its purpose in plain language.
2. Connect the concept to concrete Python classes, methods, and module boundaries.
   Show a small source excerpt, call chain, or diagram when it clarifies behavior.
   Link the actual files; the learner should not have to read them to follow along.
3. Walk through one concrete example yourself. Explain consequential units,
   timing, state changes, and failure behavior. Do not turn it into a task for the
   learner or require them to run anything.
4. Stop at the end of that subsection. Name the next subsection without teaching
   it. Advance only when the learner explicitly says to continue, next, move on,
   or asks for a particular section. A clarification, checklist request, or tool
   operation is not permission to advance. Answer questions at the same position.

Keep each turn short enough to read comfortably; introduce terms before using
them. Show concepts and code together rather than giving a conceptual course
first and an implementation course later. Use Unicode math in chat. Adjust depth
to the learner's questions without expanding into an unrelated investigation.
Honor explicit requests to skip, reorder, recap, or change the pace.

Give usable examples as part of the explanation: show local environment commands,
the memory example entry point, a minimal node and graph, predictive evaluation,
and a public deployment manifest when their subsections arrive. Explain each
example's prerequisites and expected behavior; do not require execution. Native
QC features need the platform environment, while memory fixtures can demonstrate
the local framework path. Independence from other strategy packages does not
imply a built-in historical-data vendor, brokerage simulator, or QC-free native
indicator implementation.

## Track progress

Maintain the numbered position, covered subsections, and next subsection. A
checked item means explained, not assessed or independently mastered. Keep these
separate from commands or work the learner has completed.

When a side checklist is requested, reuse the existing tracker if available;
otherwise create an editable side-panel checklist using available artifact tools
and open it beside the conversation. Include the 12 sections and 36 subsections
from the syllabus, a current-position line, and a next-subsection line. Update
only covered items and position as the tutorial proceeds. Use an inline checklist
if a side panel is unavailable; do not claim an artifact opened without a
confirmed tool result. Do not introduce recurring automations for this tracker.

On resume, reconcile the learner's latest instruction, conversation, and tracker.
Do not infer advancement from a checked box alone, and do not restart a covered
section unless requested. The existing conversation's pacing and scope persist.

## Preserve the important distinctions

- Node calculations publish Views; execution turns a final PositionView into
  broker orders. Predictive research uses GraphRuntime without execution.
- Observation/graph time and availability/receipt time have different meanings.
  Late arrivals cannot rewind graph time; schedules depend on incoming batches.
- Outputs replace whole node snapshots. Missing input or withdrawn authority is
  different from an explicit zero position, which requests an exit.
- Read fresh broker state for reconciliation. Pending exposure remains reserved
  through partial fills, unconfirmed cancellation, and uncertain submissions.
- Explain current supported capabilities from validation and relevant source.
  Memory fixtures are supplied observations and fills, not a market simulator;
  local boundary tests do not establish native API or numerical parity.
- Correct mechanics, successful compilation, and example P&L do not establish
  predictive skill or a profitable strategy. Separate forecast metrics from
  portfolio results and explain chronological evaluation and dependent forecasts.

## Scope of the tutorial

Read and explain by default. Skill invocation does not authorize dependency
installation, implementation changes, test/example execution, uploads, paid
compute, or cloud submissions. Present commands as explanations unless the
learner asks to execute them. If practical work is requested, respect repository
instructions and the authorization already given for that action. Commit, push,
release, live deployment, and capital allocation retain their separate gates.
Explain available tools without assuming credentials, account access, cloud
capacity, or a connector that is not actually exposed.
