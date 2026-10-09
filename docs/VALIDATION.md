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

## Native acceptance, 9 October 2026

Run on LEAN 2.5.0.0.18178 after the audit changes. Each row answers one
integration question; none says anything about profitability. All runs completed
without runtime errors.

The check graphs are deliberately untracked scaffolding in
`artifacts/qc-acceptance/` (project `37597459`). Every run's exact uploaded
source, including those graphs, is in its bundle snapshot under
`artifacts/qc-acceptance/artifacts/acceptance/`. That directory is git-ignored:
it is evidence on this machine, not a backup.

| Question | Backtest | Observed |
| --- | --- | --- |
| Do session-count horizons follow the exchange calendar, and can a Friday decision trade? | `a09b757e4198fbbcbe46061614c47bc3`, repeated on later source as `ccf76792f3a78eb7c898ed64453efaa3` | Daily SPY bars complete at 16:00 New York. Asked at exactly that close, `session_close` returned the next session's close: Wednesday 8 January → Friday 10 (market closed on the 9th), Friday 10 → Monday 13, Friday 17 → Tuesday 21 (holiday on the 20th). Each 16:00 decision was refused as session-closed and filled at 09:31 on the next session, including Monday after a Friday decision. |
| Is shorting permitted under LEAN's default shortable provider? | same runs | The target alternated +30% and −30%. Every short entry filled; no borrow rejection. |
| Does the rebalance band stop drift re-trading? | `de6544d12f1e9191c440fa2c82d90f0c` (band 0) and `a5a5e871104671d47ed44e61e21f1c67` (band 0.25%) | A constant 50% SPY weight on USD10m over four sessions: 953 orders and USD993.89 of fees with no band; 3 orders and USD43.89 with it. The two maintenance orders were 43 and 44 shares, about 0.25% of NAV. |
| Does a corporate action touch only the affected part of the graph? | `bd69f53bdb7d6b22315eebc2caf5b0d7` (and `821fa9dda733cb14178f1bae63edc2cb` without the relay) | June 2024, three independent readers. NVDA's split re-evaluated only the raw NVDA reader. NVDA's and SPY's dividends re-evaluated nothing (raw prices). QQQ's dividend re-evaluated only the adjusted QQQ reader. A relay downstream of the SPY position producer ran exactly once per upstream output, so that target was never withdrawn, and its single order was undisturbed. |
| Can a node work with a partly ready universe? | `5d90c2881a8229085e63ee60ee63cdbb` | A 60-name native liquidity universe with a 150-bar daily lookback, 9–13 June 2025. The `require_all=False` node evaluated on all five days with 58 of 60 members ready (57 on the last day); the unready ones were CRWV, CRCL and CHYM, all recently listed. Its strict twin was withdrawn every day with `Insufficient history`. |
| Does the public EMA example still behave the same? | `8422af7a72778f27074ce931d3235d5b` (project `37377184`) | 6 orders, USD6.00 fees, end equity USD99,652.57: identical to the 4 October run above. |

The research notebook was re-executed in the native QuantBook kernel with a
one-session horizon: nine forecasts issued and **nine scored**, where the
calendar-day horizon had left three unscorable. Output:
`artifacts/ema/native-research/2026-10-09/ema_research.ipynb`.

What these runs found, beyond confirming the changes:

- **Queued facts were being spent on data-less callbacks (fixed).** In run
  `172974f9ada6c6ac6fc3cd503c755ac2` the universe-triggered nodes were withdrawn
  with `Universe unavailable` on two of five days. LEAN had called `OnData` with a
  slice carrying no bars; the host delivered the queued universe event with the
  graph clock still at the previous close, so that morning's selection looked like
  the future and the one trigger for it was consumed. The host now holds
  callback-queued facts until a slice carries data. The repeat run above evaluated
  on all five days.
- **Adjusted prices in a backtest are already fully adjusted.** The adjusted QQQ
  window was identical before and after its ex-dividend date: LEAN applies the
  whole factor file, including later dividends, from the start. Rebuilding an
  adjusted window on a dividend is therefore harmless but changes nothing in a
  backtest. It would matter with live data, which this does not test.
- **A node re-evaluated by a lifecycle event emits nothing until its next bar.**
  The event arrives before the open while the graph clock is still at the prior
  close, so the new View's information cutoff predates the event and is rejected.
  This is the existing conservative rule, now applied to the affected node only.
- **Small accounts still make single-lot corrections.** With USD100,000 in SPY,
  one share is 0.6% of NAV, more than the 0.25% band, and the session runs made
  four one-share trades in two weeks as the exact target crossed whole shares.
  `min_trade_value` or a wider band suppresses them.
- **A stale research session answers 404.** `research/read` returned a session
  whose server no longer existed. The notebook runner now checks the server and
  requests a fresh session in that case.

Not covered by these runs: partial fills, cancellations and cancel rejections;
quotes; ETF-constituent universes; delistings and symbol changes; live data.
The session, band and lifecycle runs predate the host fix; only comments changed
in the engine after the final session run.

## Parity with plain QuantConnect algorithms

Five simple rules were each run twice in QC Cloud on 9 October 2026: once as a
native `QCAlgorithm` with no Sugarloaf code, once as a Sugarloaf graph. All five
pairs produced identical orders (fill time, symbol, quantity and fill price) and
identical ending equity.

| Rule | Orders |
| --- | ---: |
| Buy and hold | 1 |
| Three assets rebalanced to fixed weights at 10:00, with a drift band | 15 |
| Daily close above SMA(5): long, else flat (library `SMATrend`) | 12 |
| Five-minute EMA(5) above EMA(20): long, else flat (library `EMASpread`) | 40 |
| Buy at 10:00, sell at 15:30 every session | 18 |

The pairs are an untracked pytest module, `artifacts/qc-parity/test_parity.py`,
run on demand with `python -m pytest artifacts/qc-parity/test_parity.py -v`. They
need QC credentials and about four minutes of cloud time.

Getting the pairs to agree showed how Sugarloaf differs from a naive native
algorithm. Each is intended behaviour that a strategy author needs to know:

- **A wall-clock schedule without a calendar fires at the next open after a
  weekend or holiday.** `Schedule(at=time(10, 0))` occurs every calendar day and a
  missed occurrence fires at the next data tick. Use `calendar=instrument` for
  "each session".
- **Each order is sized when it is sent.** In a multi-asset rebalance, later
  orders see NAV after the earlier orders' slippage and fees.
- **Warm-up bars are not decisions.** The first decision comes from the first bar
  completed after the start, never from history used to prepare an indicator.
- **A daily-close signal trades at the next session's first minute bar,** because
  the market is closed when the daily bar completes.
- **An adjusted-price signal is withdrawn on its ex-dividend day** until its next
  bar. The daily pair's dates avoid SPY's ex-dividend days for that reason, so this
  difference is known but not exercised by the pairs.

The pairs do not cover partial fills, cancellations, shorting, universes or
anything outside regular-session market orders.

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
- No live-deployment/restart recovery or durable state. Memory fixtures are not a
  market simulator.
- Session-count horizons, the rebalance band, scoped lifecycle invalidation,
  partial-universe readiness and default-provider shorting each have one native
  backtest (see the 9 October 2026 acceptance above). That is acceptance of the
  mechanism on one date range, not broad coverage.
- No independent-sample inference is justified for overlapping forecast revisions.
