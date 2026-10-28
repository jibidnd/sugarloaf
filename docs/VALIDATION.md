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
| Does `session_open` return regular opens on the exchange calendar? | `f00603382d4e2b8d1caa8c74c5825f12` (Lab project 38086843, momentum view native check, 2022-11-01 to 2023-06-30) | Asked at each 16:00 New York close, `session_open(SPY, now, 1)` and `(…, 22)` returned 09:30 New York (13:30 UTC) opens only, never pre-market. Friday → Monday (65.5 hours); Friday 26 May 2023 → Tuesday 30 May (Memorial Day skipped, 89.5 hours). Every interval ending inside the window spanned 21 sessions, including Juneteenth; the one from 2 June ends 6 July, counting the 3 July early close and skipping 4 July. Opens in standard time (14:30 UTC) were not exercised. |
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
  whole factor file, including later dividends, from the start. The rule these
  runs exercised rebuilt that window and withdrew its reader anyway. It has since
  moved into the QuantConnect adapter, which now rebuilds nothing on a dividend
  (see [corporate actions](technical/ADAPTERS.md#corporate-actions-and-data-resets)
  and the parity section below).
- **A node re-evaluated by a lifecycle event emits nothing until its next bar.**
  The event arrives before the open while the graph clock is still at the prior
  close, so the new View's information cutoff predates the event and is rejected.
  This is the existing conservative rule, now applied to the affected node only.
  Since the adapter change it follows a split, not a dividend.
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

## Native feeds, order types and feedback order, 11–12 October 2026

Run on LEAN 2.5.0.0.18178 against the working tree on `403edf9`, after the
adapter began subscribing each security at the coarsest native resolution its
readers need and the engine stopped deciding when an order may be sent.

**What LEAN does** (plain algorithm, no Sugarloaf code; project `37597459`,
backtest `dfe6a1846c154ee680ff758bac880fe2`, source in
`artifacts/qc-acceptance/probe/`):

| Question | Observed |
| --- | --- |
| When does a daily bar arrive? | At 16:00 New York with `daily_precise_end_time` at its default, and an `IdentityDataConsolidator` on a daily subscription emits it then |
| Which subscription feeds a consolidator when a security has two? | The first one added, whatever its resolution: daily-first received daily bars, minute-first received minute bars |
| What happens to a market order sent while the exchange is closed? | It becomes a market-on-open order and fills at the next opening price, from a daily bar at 16:00 and from a minute security at 16:00:00 alike |
| When is a market-on-close order accepted? | At 15:40 it filled at the close. At 15:55 the ticket came back invalid, with order ID −10 and no order event |
| When is a market-on-open order accepted? | On a daily security at 09:32 it was accepted and filled at the next day's open. On a minute security during the session it came back invalid |
| Does a daily subscription with fill-forward add data? | Yes: it puts a fill-forward bar in every minute slice when the algorithm also has a minute subscription |

**A cross-sectional graph on daily bars.** A 250-stock native liquidity universe
with a daily 757-bar lookback, a ranking node and a long-short converter,
January–February 2024, USD 10 million (Sugarloaf Lab's short-reversal replay,
project `37695106`). Each row is one backtest of the same graph.

| Change | Backtest | Observed |
| --- | --- | --- |
| Before: minute feeds | `1a6521a198c57b5acd06c1dfdcc833ef` | 599 orders, +2.62%. 16.3 million data points, 249 seconds |
| Daily feeds, default policy | `b2f4db9eeb1f6a29d278fc5a74d8f4dd` | 0.49 million data points, 29 seconds. 1,560 orders, none filled: each was sent at the close, held by LEAN for the next open, and cancelled at midnight by the engine's five-minute order lifetime, on every night that a corporate-action slice caused a reconciliation |
| No engine cancel timer | `72269996aed86af5a5ef909271ae1a4a` | 961 orders, none filled: each day order expired at the next close, the instant the daily bar carrying its fill price arrived |
| Market-on-open, good till cancelled | `fb4da6661e94af844c84c302df2a01c3` | 533 fills. At one close, 75 further orders: a drift order sent while the previous day's 44 orders were filling made LEAN fill the rest before the call returned; the engine then read holdings that included those fills while their feedback was queued, sent 37 orders reversing them and 37 undoing those. A new target cancelled all 75 in the same instant |
| Feedback applied before each broker read | `01e7f614fee5c54084739ac094c41d2a` | The 75 became 1 (the drift order). Same fills and ending equity |
| Stocks-only universe filter | `2ecef86ce87ef62714562b3f6acfe11f` | 619 orders, +2.61%, 0.51 million data points, 28.5 seconds |
| A split leaves weight targets and rankings standing; orders good till cancelled by default | `8f789a06b7d9f988f8f9f84cb395cfb1` | 598 orders, +2.83%, no runtime diagnostics. The plain daily-bar algorithm made 587 orders and +2.86% on the same window (`148f18fa47294c689c46c2d41e09842e`) |

- **Agreement with plain LEAN.** To 22 February the graph made 261 fills and
  +0.98% (`28c607e2c8714f2d5ce449572f440a8e`); a plain daily-bar algorithm
  applying the same rules made 269 fills and +1.01%
  (`5b6894ac6648f411a3175614fb9a35a5`). On the first trading day 37 of 39 symbols
  were common and 36 had identical quantities. The two differ in how they leave
  funds out of the universe.
- **Thirteen years.** The same graph from January 2014 to September 2026, on
  the final source (`1e48b54acf52fd78fa5b7693d4c96e45`): 3,205 sessions, 129,335
  orders, 35.8 million data points, 48 minutes, no runtime errors. It compounded
  at 7.25% a year to USD 24.42 million with a 12.2% largest drawdown; the plain
  daily-bar algorithm at 7.35% to USD 24.70 million with 11.7%, on 133,017
  orders in 33 minutes (`980573343a17cd669e941113e0004b50`).
- **Orders waiting through a split are sized again.** Apple and Tesla split on
  31 August 2020. In the thirteen-year run the orders sent for both at Friday's
  close were cancelled at midnight on the 31st and replaced in the same instant
  (878 and 709 adjusted shares), and the replacements filled at that morning's
  open. The weight target and the rankings stood throughout.
- **A split used to cost a session.** Walmart split on 26 February. Under the
  rule then in force the runtime withdrew every View naming it at midnight, the
  target went with them, and the 56 orders waiting for that morning's open were
  cancelled; the graph issued a new target at that day's close. With minute feeds
  the same gap had lasted one minute. A split now withdraws only Views stated in
  units of the instrument, and in the last row of the table those 56 orders fill.
  Under the earlier rule the thirteen-year run recorded 131 not-ready
  diagnostics (`c66fd34ee534f243113a78683197b744`, 7.26% a year); under this one
  62; splits no longer cause any.
- **A target that expires at the next close is cancelled and filled in one
  instant.** On daily bars LEAN reports yesterday's orders at today's close. If
  the target expires then, the engine asks to cancel the remaining orders after
  the first fill; LEAN has them pending cancellation and fills them in the same
  pass (event sequence submitted, cancel pending, filled, on about 45 orders a
  day). The outcome is the intended one, and it rests on LEAN's ordering.
- **New members have a price.** The security initializer now seeds a new
  security with its last known price; without it a member selected in the
  morning could not be valued at that day's close.
- **A security can be listed twice in a day's selection data.** A run from 2014
  stopped on 6 February 2014 with one stock twice in a ranking
  (`c22f4aa9b27f2a21c551a5c4f75077e0`). Selection now returns each identity once,
  and the first quarter of 2014 completes (`1b9628258b7eaa4228bb1c0d92718b4f`).

Not covered: hourly feeds, quotes on a coarser feed, a feed made finer after its
security was subscribed (the adapter refuses it), a market-on-close policy inside
a graph, and a platform refusal reaching the engine from a live ticket. The
refusal path, the feed planning and the order-type translation have local tests
only.

## Fundamental fields and quote normalization, 28 October 2026

Run on LEAN 2.5.0.0.18207 against the working tree of `feat/fundamentals-quotes`, after
`FundamentalRecord` gained the Morningstar sector code, the REIT flag and declared generic
fields, and `QuotesRequirement` gained `normalization`. The catalogue was generated from
the same LEAN version on a research kernel. The check graph is untracked scaffolding in
`artifacts/qc-acceptance/fundamentals_quotes/` (project `37597459`); its bundle snapshots
and the research comparison are kept there.

| Question | Backtest | Observed |
| --- | --- | --- |
| Does a `FundamentalUniverse` receive the typed sector and REIT flag and its declared fields, multi-period ones by period? | `2873c1f58a021b877ec19e456a4fd4ee` (8–14 August 2024; first run `8a9e42a061c562cc890392b97574635f` had its report cut at QC's 8,000-character log limit) | Five selections at 04:00 UTC, about 9,650 records each: about 5,370 without a sector (funds and other securities without company data), 194 REITs. The rule declared ROE (one year), P/E, twelve-month revenue, IPO date and legal name and kept the five largest technology non-REITs by dollar volume (NVDA, AAPL, MSFT, AMD, SMCI or TSM). AAPL on 8 August: ROE 1.511, P/E 31.94, revenue USD383.3bn, IPO 1980-12-12, "Apple Inc". Missing values arrived as None; twelve-month revenue was 0 for about 490 records. |
| Are adjusted quotes on one scale through warm-up and streaming, across an ex-dividend date? | same run | The members' adjusted minute quotes (about six sessions of lookback) were read each day at 15:59. Every hourly AAPL quote, 30 July to 14 August, equalled QuantBook's adjusted minute history at the same bar end (77 of 77): those before 8 August came from warm-up history, the rest were streamed to the universe member. Against raw history the ratio stepped from 0.99056 to 0.99170 at the 12 August ex-date. Before the change, warm-up history was raw while streamed member quotes were adjusted. |

Not covered: explicit (non-universe) quote subscriptions natively, split-adjusted and
total-return quotes, and LEAN versions after 2.5.0.0.18207 (regenerate the catalogue when
the fundamental classes change). LEAN's boolean fields have no missing state: `is_reit` is
False for a security LEAN has no company data for.

## Parity with plain QuantConnect algorithms

Five simple rules are each run twice in QC Cloud: once as a native `QCAlgorithm`
with no Sugarloaf code, once as a Sugarloaf graph. All five pairs produce
identical orders (fill time, symbol, quantity and fill price) and identical
ending equity. They were first run on 9 October 2026 and rerun on 12 October
2026 after the feed and order changes above, with the same order counts.

Two things changed on the native side for the rerun, because the graph no longer
waits for the exchange to open before sending an order. The daily rule now
subscribes daily bars and sends its market order at the close, which LEAN holds
for the next open; and the five-minute rule sends the order from its 16:00 bar at
16:00. Before, both sides traded those signals at 09:31. With day orders, the
default at the time, the daily graph placed 56 orders and filled none
(`c517d09fe788a304179166e140f9df18`); orders are now good till cancelled by
default.

| Rule | Orders |
| --- | ---: |
| Buy and hold | 1 |
| Three assets rebalanced to fixed weights at 10:00, with a drift band | 15 |
| Daily close above SMA(5): long, else flat (library `SMATrend`), across two SPY ex-dividend days | 23 |
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
- **A daily-close signal trades at the next session's opening price.** The order
  is sent at the close and LEAN holds it for the open. Orders are good till
  cancelled by default; a day order sent at the close on daily bars expires
  unfilled.
- **A dividend does not disturb a signal.** The daily pair runs from 3 September
  2024 to 17 January 2025, across SPY's ex-dividend days of 20 September and
  20 December. The 19 September close crosses above the average, so a buy is due
  at the ex-date's open, and both sides make it. Run against the previous commit
  (`4a2907d`), the same pair failed: Sugarloaf withdrew the adjusted-price signal
  on the ex-date and bought one session late, on 23 September. The QuantConnect
  adapter now rebuilds nothing on a dividend, so nothing is withdrawn. A split on
  raw prices is not exercised by the pairs.

The pairs do not cover partial fills, cancellations, shorting, universes or
market-on-close orders.

## Capability limits

- QC trading: US equities, regular-session day or good-till-cancelled market,
  market-on-open and market-on-close orders; minute, hourly or daily feeds chosen
  from the declared intervals, with native consolidation to coarser ones, and
  synchronous daily universes.
- A graph acts only when data arrives. One that reads only daily bars is evaluated
  once a session, at the close.
- On daily bars in a QuantConnect backtest a `day` order sent at the close never
  fills. The default is `gtc`.
- Native indicators: QC SMA, EMA, RSI and Donchian. No memory formula fallback or
  cross-provider parity promise. Older raw data cannot rewind native indicators.
- QuantBook: explicit instruments, bars and native indicators; split/dividend
  facts replay at effective boundaries, and a split resets raw-price resources
  before reads.
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
