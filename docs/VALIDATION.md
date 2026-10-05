# Validation and limits

## Local verification

Local regression coverage includes immutable View identities, graph
wiring, late/equal-time data, expiry/lifecycle invalidation, schedules, resource
ownership, native-boundary prefill/consolidation, causal replay, outcome alignment,
all predictive scoring families, native-indicator signal nodes, simple aggregation/position conversion, explicit
deployment bundles, and order/fill/cancel flows.
Tests also cover structural custom-provider replay, typed split/dividend
admission and resource reset, immutable deployment source/result evidence,
historical run lookup, submission failures, and complete log pagination. Current
test counts and check outcomes belong with each change's verification record.
The public EMA notebook's code cells parse; its supplied outputs are empty.
The manifest-driven public run interface and QC-local cloud tooling have local
regressions and native acceptance for the EMA deployment and research notebook
described below. Cloud evidence applies to their saved source snapshots.

Current source is locally tested. Cloud evidence applies
to the recorded source manifest below; it does not certify other source snapshots.
The EMA QuantBook replay runs in the native research kernel. Local adapter tests
use injected API boundaries; capabilities outside the observed native paths still
need platform acceptance.

## Cloud evidence

Project `37377184`, January 6-10, 2025, starting equity USD100,000. The EMA run
completed without runtime errors. LEAN 2.5.0.0.18155; Python 3.11.14; NumPy 1.26.4;
SciPy 1.13.1. These are integration checks, not evidence of predictive value.

| Strategy | Backtest ID | Orders | Fees | End equity |
| --- | --- | ---: | ---: | ---: |
| Native EMA, SPY, 5-minute source | `7e6db1d6422d2f17f4f11d1842eb304d` | 6 | $6.00 | $99,652.57 |

The EMA run processed 1,560 data batches and recorded six complete cumulative-fill
updates. It exercises native subscription, consolidation, indicator prefill,
graph evaluation, market submission, ticket feedback and reporting.
Source manifest SHA-256:
`6f2003e9694b0af1e9e8ee84192531ed7b1ae0e73d841e9bab1c189d1c64fb36`.
Sources, compile/submission identity, results, logs and `acceptance.json` are
retained under `artifacts/ema/`.

The exact bundled EMA research notebook completed all four code cells in the same
project's native QuantBook kernel. January 6-18, 2025 replay issued nine forecasts
with no readiness diagnostics. Outcome evaluation scored six and retained three
unavailable exact anchors; none were issued after their target end. The output
notebook is `artifacts/ema/native-research/ema_research.ipynb`. It covers daily
native EMA replay, provenance and outcome evaluation, not corporate-action
acceptance or cross-provider numerical parity.
Public runs use ignored per-manifest artifact directories; each result retains
its exact source/parameter/compile identity.
See [the guide](GUIDE.md#4-deploy-and-inspect) for retrieval and inspection.

## Capability limits

- QC trading: US equities, regular-session day market orders; documented minute
  through daily bar/quote consolidation and synchronous daily universes.
- Native indicators: QC SMA, EMA, RSI and Donchian. No memory formula fallback or
  cross-provider parity promise. Older raw data cannot rewind native indicators.
- QuantBook: explicit instruments, bars and native indicators; split/dividend
  facts replay at effective boundaries and reset affected resources before reads.
  `price_return` outcome anchors request split-adjusted history; total-return
  anchors request total-return history. Historical receipt is assumed equal to
  observation end. Exact missing outcome anchors stay unavailable. Native numerical
  parity, native corporate-action replay and adjustment-vintage correctness remain
  unverified. Native notebook acceptance covers only the interval and instruments
  above; injected-boundary tests establish the additional local causal contracts.
- The EMA run contains no partial fills or cancellations. Native cancellation
  rejection/retry and partial-fill sequences remain separate acceptance checks.
- No live-deployment/restart recovery, durable state or bounded identity/intent
  retention. Memory fixtures are not a market simulator.
- No independent-sample inference is justified for overlapping forecast revisions.
