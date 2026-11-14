"""Plain-LEAN cost of the reads Sugarloaf's QuantConnect adapter makes for a daily panel.

No Sugarloaf code runs here. The algorithm selects the move-size node's panel natively (the
200 most liquid US stocks priced at $5 or more with company fundamentals) beside its 14
equity funds and the VIX, then measures, one task per session so that no single time loop
nears the platform's 10-minute limit:

* the 4,900-bar daily history of the 214 securities in the four shapes LEAN offers (one
  typed call for all symbols, a DataFrame, one typed call per symbol, a bar-count request),
  with the Python-side work the adapter adds per bar measured separately from the read;
* the EODHD earnings calendar over 6,800 days, as typed objects and as the DataFrame the
  adapter reads, with the adapter's pandas reduction timed on its own;
* indicator warm-up with LEAN's helper for each security;
* fundamentals reads and split/dividend history for the panel.

Every result is logged as one JSON line prefixed ``NATIVE_DIAG``; ``collect.py`` gathers
them from the retained backtest logs. Numbers are wall time on the backtest node, so they
compare runs on the same node class only.
"""

# ruff: noqa: F405, I001  (names come from the platform's AlgorithmImports star import,
# which must precede the clock import below)
import json
from datetime import datetime, timedelta

from AlgorithmImports import *  # noqa: F401,F403  (the platform's own import style)
from QuantConnect.DataSource import EODHDUpcomingEarnings

# The platform's star import binds ``time`` to datetime.time; the clock is imported after it.
import time as wall  # noqa: E402

# The move-size node's funds (sugarloaf_lab/nodes/move_size_global/node.py).
FUNDS = ("SPY", "QQQ", "IWM", "DIA", "RSP", "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU",
         "XLV", "XLY")
PANEL_COUNT = 200          # stocks in the panel
LOOKBACK_BARS = 4900       # the node's daily lookback
# The adapter asks for count * 1.45 + 10 calendar days of daily history for a lookback
# (sugarloaf/adapters/quantconnect/lean.py, daily_histories).
SPAN_DAYS = int(LOOKBACK_BARS * 1.45) + 10
EARNINGS_DAYS = 6800       # the node's earnings-calendar lookback
BUDGET = 420.0             # seconds a task may spend before it stops and reports what it has


class NativeHistoryCost(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2021, 1, 4)
        self.set_end_date(2021, 1, 22)
        self.set_cash(1_000_000)
        # The adapter's settings for the panel: daily feed, adjusted prices, no fill-forward.
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.ADJUSTED
        self.universe_settings.fill_forward = False
        self._universe = self.add_universe(self._select)
        self.funds = [self.add_equity(t, Resolution.DAILY, fill_forward=False).symbol
                      for t in FUNDS]
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        # The earnings calendar is one dataset for every security, added once.
        self.earnings = self.add_data(EODHDUpcomingEarnings, "earnings").symbol
        self.members = None
        self.results = []
        # One task per session; each result is logged as it completes.
        self.tasks = [
            self.bars_bulk_typed,
            self.bars_dataframe,
            self.bars_count_based,
            self.bars_per_symbol,
            self.earnings_typed,
            self.earnings_dataframe,
            self.indicator_warmup,
            self.fundamentals_and_actions,
        ]
        self.record("initialize", {"lean": str(Globals.version), "span_days": SPAN_DAYS,
                                   "lookback_bars": LOOKBACK_BARS, "earnings_days": EARNINGS_DAYS})

    # ---- universe ---------------------------------------------------------------------------

    def _select(self, fundamental):
        """The panel, selected once at the first selection and then left unchanged."""
        if self.members is not None:
            return Universe.UNCHANGED
        began = wall.perf_counter()
        rows, total = [], 0
        for f in fundamental:
            total += 1
            if f.has_fundamental_data and f.price >= 5 and f.volume > 0 and f.dollar_volume > 0:
                rows.append(f)
        # The adapter ranks by dollar volume with the stable identity breaking ties and keeps
        # each identity once (sugarloaf/adapters/base.py, select and _first_distinct).
        rows.sort(key=lambda f: (-f.dollar_volume, str(f.symbol.id)))
        seen, members = set(), []
        for f in rows:
            if f.symbol in seen:
                continue
            seen.add(f.symbol)
            members.append(f.symbol)
            if len(members) == PANEL_COUNT:
                break
        self.members = members
        self.record("selection", {"rows": total, "eligible": len(rows), "members": len(members),
                                  "seconds": round(wall.perf_counter() - began, 3)})
        tickers = [s.value for s in members]
        for first in range(0, len(tickers), 100):
            self.record("members", {"first": first, "tickers": tickers[first:first + 100]})
        return members

    # ---- one task per session ---------------------------------------------------------------

    def on_data(self, data):
        # Only a slice that carries a session's bars starts a task: the panel is subscribed
        # and the clock stands at a close, as it does for the adapter's first evaluation.
        if self.members is None or not data.bars.contains_key(self.funds[0]) or not self.tasks:
            return
        task = self.tasks.pop(0)
        began = wall.perf_counter()
        try:
            result = task()
        except Exception as error:  # noqa: BLE001  (a failed task is a result, not a stop)
            result = {"error": f"{type(error).__name__}: {error}"[:600]}
        result["seconds"] = round(wall.perf_counter() - began, 2)
        self.record(task.__name__, result)

    def on_end_of_algorithm(self):
        self.record("summary", {"tasks_completed": [r["task"] for r in self.results],
                                "tasks_left": [t.__name__ for t in self.tasks]})

    def record(self, name, payload):
        payload = {"task": name, "at": self.time.isoformat(), **payload}
        self.results.append(payload)
        self.log("NATIVE_DIAG " + json.dumps(payload, default=str))

    # ---- helpers ----------------------------------------------------------------------------

    @property
    def panel(self):
        return list(self.members) + list(self.funds)

    def span(self):
        # Algorithm-local endpoints, as the adapter converts its UTC cutoff before asking.
        end = self.time
        return end - timedelta(days=SPAN_DAYS), end

    def typed_history(self, symbols, start, end):
        return self.history[TradeBar](symbols, start, end, Resolution.DAILY, fill_forward=False,
                                      data_normalization_mode=DataNormalizationMode.ADJUSTED)

    # ---- tasks ------------------------------------------------------------------------------

    def bars_bulk_typed(self):
        """One typed call for all symbols (the adapter's daily_histories), three ways.

        enumerate_only: iterate the result and count bars, no per-bar Python work.
        python_fields: a fresh call, reading the six fields of every bar as Python values,
            which is what the adapter's Bar conversion does.
        python_fields_and_utc: a fresh call adding the two exchange-to-UTC conversions the
            adapter makes for each bar.
        """
        start, end = self.span()
        out = {}
        began = wall.perf_counter()
        bars_seen = slices = 0
        for bars in self.typed_history(self.panel, start, end):
            slices += 1
            bars_seen += bars.count
        out["enumerate_only"] = {"seconds": round(wall.perf_counter() - began, 2),
                                 "bars": bars_seen, "slices": slices}
        began = wall.perf_counter()
        bars_seen = 0
        for bars in self.typed_history(self.panel, start, end):
            for symbol, bar in bars.items():
                # Read as the adapter does; the tuple itself is discarded.
                (float(bar.open), float(bar.high), float(bar.low), float(bar.close),
                 float(bar.volume), bool(bar.is_fill_forward))
                bars_seen += 1
        out["python_fields"] = {"seconds": round(wall.perf_counter() - began, 2), "bars": bars_seen}
        zone = self.securities[self.funds[0]].exchange.time_zone
        began = wall.perf_counter()
        bars_seen = 0
        for bars in self.typed_history(self.panel, start, end):
            for symbol, bar in bars.items():
                (float(bar.open), float(bar.high), float(bar.low), float(bar.close),
                 float(bar.volume), bool(bar.is_fill_forward),
                 Extensions.convert_to_utc(bar.time, zone),
                 Extensions.convert_to_utc(bar.end_time, zone))
                bars_seen += 1
        out["python_fields_and_utc"] = {"seconds": round(wall.perf_counter() - began, 2),
                                        "bars": bars_seen}
        return out

    def bars_dataframe(self):
        """The same read as a DataFrame, LEAN's most common history shape."""
        start, end = self.span()
        began = wall.perf_counter()
        frame = self.history(self.panel, start, end, Resolution.DAILY, fill_forward=False,
                             data_normalization_mode=DataNormalizationMode.ADJUSTED)
        built = wall.perf_counter() - began
        began = wall.perf_counter()
        closes = frame["close"].unstack(0)
        return {"dataframe_seconds": round(built, 2), "rows": int(len(frame)),
                "unstack_seconds": round(wall.perf_counter() - began, 2),
                "columns": int(closes.shape[1]), "sessions": int(closes.shape[0])}

    def bars_count_based(self):
        """A bar-count request: LEAN finds the span per security from its calendar."""
        began = wall.perf_counter()
        bars_seen = 0
        for bars in self.history[TradeBar](self.panel, LOOKBACK_BARS, Resolution.DAILY,
                                           fill_forward=False,
                                           data_normalization_mode=DataNormalizationMode.ADJUSTED):
            bars_seen += bars.count
        return {"seconds_read": round(wall.perf_counter() - began, 2), "bars": bars_seen}

    def bars_per_symbol(self):
        """One typed call per symbol: the adapter's path before commit 2a9f6ad."""
        start, end = self.span()
        began = wall.perf_counter()
        bars_seen = calls = 0
        for symbol in self.panel:
            for bar in self.typed_history(symbol, start, end):
                bars_seen += 1
            calls += 1
            if wall.perf_counter() - began > BUDGET:
                break
        return {"calls": calls, "bars": bars_seen, "complete": calls == len(self.panel)}

    def earnings_years(self):
        end = self.time
        year = end - timedelta(days=EARNINGS_DAYS)
        while year < end:
            stop = min(end, datetime(year.year + 1, 1, 1))
            yield year, stop
            year = stop

    def earnings_typed(self):
        """The calendar a year at a time as typed objects, touching one field per row."""
        began = wall.perf_counter()
        per_year, rows = [], 0
        complete = True
        for year, stop in self.earnings_years():
            if wall.perf_counter() - began > BUDGET:
                complete = False
                break
            t = wall.perf_counter()
            count = 0
            for item in self.history[EODHDUpcomingEarnings](self.earnings, year, stop):
                item.symbol  # one field, as a consumer filtering by security would read
                count += 1
            per_year.append([year.year, count, round(wall.perf_counter() - t, 2)])
            rows += count
        return {"per_year": per_year, "rows": rows, "complete": complete}

    def earnings_dataframe(self):
        """The calendar as the adapter reads it: a DataFrame per year, then its reduction.

        Each year gives [year, rows, rows kept, read seconds, reduction seconds]. The
        reduction is the adapter's (lean.py, earnings): one listing per security and day,
        then only the rows where a security's listing changes.
        """
        began = wall.perf_counter()
        per_year, rows, kept = [], 0, 0
        complete = True
        for year, stop in self.earnings_years():
            if wall.perf_counter() - began > BUDGET:
                complete = False
                break
            t = wall.perf_counter()
            frame = self.history(EODHDUpcomingEarnings, self.earnings, year, stop)
            read = wall.perf_counter() - t
            t = wall.perf_counter()
            count = 0 if frame is None else int(len(frame))
            changed = 0
            if count:
                frame = frame.reset_index()
                frame = frame.assign(
                    sid=frame["symbol"].astype(str),
                    day=frame["time"].dt.normalize(),
                    listing=frame["reportdate"].astype(str) + "|" + frame["reporttime"].astype(str),
                ).sort_values(["sid", "day", "reportdate"], kind="stable")
                frame = frame.drop_duplicates(["sid", "day"], keep="first")
                change = ((frame["sid"] != frame["sid"].shift())
                          | (frame["listing"] != frame["listing"].shift()))
                changed = int(change.sum())
            per_year.append([year.year, count, changed, round(read, 2),
                             round(wall.perf_counter() - t, 2)])
            rows += count
            kept += changed
        return {"per_year": per_year, "rows": rows, "kept": kept, "complete": complete}

    def indicator_warmup(self):
        """LEAN's warm-up helper for one indicator per security, then indicator history."""
        began = wall.perf_counter()
        ready = 0
        for symbol in self.panel:
            sma = SimpleMovingAverage(252)
            self.warm_up_indicator(symbol, sma, Resolution.DAILY)
            ready += int(sma.is_ready)
        helper = wall.perf_counter() - began
        began = wall.perf_counter()
        sma = SimpleMovingAverage(252)
        history = self.indicator_history(sma, self.funds[0], LOOKBACK_BARS, Resolution.DAILY)
        return {"warm_up_indicator_seconds": round(helper, 2), "indicators": len(self.panel),
                "ready": ready, "indicator_history_seconds": round(wall.perf_counter() - began, 2),
                "indicator_history_points": int(history.count)}

    def fundamentals_and_actions(self):
        """Fundamentals from the securities, the bulk fundamentals call, and split and
        dividend history over the span for every symbol (the research adapter's read)."""
        began = wall.perf_counter()
        sectors = 0
        for symbol in self.members:
            record = self.securities[symbol].fundamentals
            if record is not None and record.has_fundamental_data:
                sectors += int(record.asset_classification.morningstar_sector_code or 0) > 0
        direct = wall.perf_counter() - began
        began = wall.perf_counter()
        bulk = self.fundamentals(list(self.members))
        bulk_seconds = wall.perf_counter() - began
        start, end = self.span()
        began = wall.perf_counter()
        splits = dividends = symbols = 0
        for symbol in self.panel:
            splits += sum(1 for _ in self.history[Split](symbol, start, end, Resolution.DAILY))
            dividends += sum(1 for _ in self.history[Dividend](symbol, start, end,
                                                               Resolution.DAILY))
            symbols += 1
            if wall.perf_counter() - began > BUDGET:
                break
        return {"direct_seconds": round(direct, 3), "with_sector": sectors,
                "bulk_seconds": round(bulk_seconds, 3), "bulk_records": len(list(bulk)),
                "actions_seconds": round(wall.perf_counter() - began, 2),
                "actions_symbols": symbols,
                "splits": splits, "dividends": dividends}
