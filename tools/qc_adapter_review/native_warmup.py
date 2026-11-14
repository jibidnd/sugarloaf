"""Plain-LEAN cost of the platform's own warm-up for the move-size panel, and the behaviour
of ``train`` inside a daily backtest.

No Sugarloaf code runs here. The 214 securities of the panel that ``native_history.py``
selected on 4 January 2021 (its ``members`` records, pasted below as tickers) and the VIX
are subscribed daily, and ``set_warm_up(4900, Resolution.DAILY)`` replays 4,900 sessions
of history through ``on_data`` before the start date: LEAN's "indirect history request".
The algorithm counts the callbacks and bars it receives during warm-up and times the
warm-up end to end. With the ``earnings`` parameter on, the EODHD earnings calendar is
subscribed too, so the warm-up also streams 19 years of the whole market's listings.

On the second session after warm-up the algorithm calls ``self.train`` with a function
that sleeps 75 seconds and logs the algorithm time it ran at. The platform documents
``train`` as the way to exceed the 10-minute time loop; the log shows when a training
callback requested from ``on_data`` actually runs in a daily backtest (LEAN runs
scheduled events at the start of a time step, before ``on_data``), and the platform's
own warning that the callback crossed one minute confirms the extra-time request.

On the third session it reads one year (2020) of the EODHD earnings calendar in the typed
shapes LEAN offers, to settle why the typed read in ``native_history.py`` returned no rows
where the DataFrame read returned 170,200; the calendar is subscribed for this whether or
not it was streamed through the warm-up.

Every result is logged as one JSON line prefixed ``NATIVE_DIAG``.
"""

# ruff: noqa: F405, I001  (names come from the platform's AlgorithmImports star import,
# which must precede the clock import below)
import json
from datetime import datetime, timedelta

from AlgorithmImports import *  # noqa: F401,F403  (the platform's own import style)
from QuantConnect.DataSource import EODHDUpcomingEarnings

# The platform's star import binds ``time`` to datetime.time; the clock is imported after it.
import time as wall  # noqa: E402

LOOKBACK_BARS = 4900
TRAIN_SLEEP = 75.0  # seconds; crosses the one-minute mark at which LEAN asks for more time

# Filled from native_history.py's "members" records for 2021-01-04 (200 stocks), plus the
# 14 funds. Tickers are those of 4 January 2021; LEAN maps them to the same securities.
TICKERS = [
    "TSLA", "AAPL", "AMZN", "NIO", "BABA", "MSFT", "FB", "BIDU", "NFLX", "NVDA", "MRNA",
    "AMD", "BA", "ZM", "GOOGL", "MU", "FUBO", "GOOG", "BTBT", "SQ", "SNOW", "INTC", "CRM",
    "V", "DIS", "QS", "PYPL", "PFE", "T", "JPM", "MA", "ENPH", "XOM", "SHOP", "PLTR", "BILI",
    "AAL", "BRK.B", "XPEV", "BAC", "WDC", "WMT", "PTON", "JNJ", "SNAP", "LRCX", "C", "VZ",
    "QCOM", "CVX", "ROKU", "UNH", "DKNG", "ADBE", "UBER", "DD", "HD", "JD", "CSCO", "RIOT",
    "FDX", "TTD", "GILD", "ABBV", "WFC", "COST", "CMCSA", "LMND", "MCD", "PG", "CCL", "CRWD",
    "UAL", "GS", "TSM", "BMY", "TWLO", "PLUG", "GE", "BLNK", "AZN", "DOCU", "BLDR", "AMAT",
    "RCL", "GM", "LMT", "TXN", "XL", "TMO", "KO", "TGT", "IBM", "ORCL", "LI", "REGN", "MRK",
    "FCX", "AVGO", "IFF", "PINS", "MELI", "OKTA", "GOLD", "AMGN", "PANW", "ABNB", "DHR",
    "NVAX", "ATVI", "NKE", "UPS", "TDOC", "FIS", "INTU", "CRSP", "DASH", "FCEL", "AMT", "M",
    "NCLH", "LOW", "UNP", "CVS", "ETSY", "SBUX", "MARA", "BKNG", "NOW", "RNG", "F", "ATUS",
    "DAL", "MS", "SPGI", "ACN", "WDAY", "BYND", "MMM", "CHWY", "AI", "MDLZ", "ABT", "TJX",
    "NEE", "ISRG", "COP", "DXCM", "CAT", "MO", "PEP", "CMG", "DG", "RTX", "ZS", "MDT", "HON",
    "CVNA", "FSLY", "CPRI", "MSTR", "MGM", "TMUS", "ADSK", "LULU", "SGEN", "W", "LLY", "WBA",
    "VRTX", "VTRS", "CSX", "GPN", "GD", "OXY", "TWTR", "BLK", "EVRG", "NET", "NNDM", "WKHS",
    "WORK", "CCI", "FVRR", "CI", "RUN", "LUV", "CME", "ANTM", "EA", "OSTK", "FTCH", "ADI",
    "BP", "BIIB", "EDU", "LIN", "WYNN", "EBON", "MTCH", "SPY", "QQQ", "IWM", "DIA", "RSP",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY",
]


class NativeWarmupCost(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2021, 1, 4)
        self.set_end_date(2021, 1, 12)
        self.set_cash(1_000_000)
        self.with_earnings = (self.get_parameter("earnings") or "off") == "on"
        self.symbols = [self.add_equity(t, Resolution.DAILY, fill_forward=False).symbol
                        for t in TICKERS]
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        # The calendar is always subscribed for the typed-read check on session three; with
        # the parameter off it is added after warm-up so the warm-up streams bars only.
        self.earnings = None
        if self.with_earnings:
            self.earnings = self.add_data(EODHDUpcomingEarnings, "earnings").symbol
        self.set_warm_up(LOOKBACK_BARS, Resolution.DAILY)
        self.warm_calls = self.warm_bars = self.warm_listings = 0
        self.first_warm_time = None
        self.sessions = 0
        self.trained = []
        self.started = wall.perf_counter()
        self.record("initialize", {"lean": str(Globals.version),
                                   "securities": len(self.symbols) + 1,
                                   "earnings": self.with_earnings, "lookback_bars": LOOKBACK_BARS})

    def on_data(self, data):
        if self.is_warming_up:
            # Count what the warm-up streams; touch nothing else, so the time is LEAN's.
            self.warm_calls += 1
            self.warm_bars += data.bars.count
            if self.with_earnings:
                self.warm_listings += data.get(EODHDUpcomingEarnings).count
            if self.first_warm_time is None:
                self.first_warm_time = self.time.isoformat()
            return
        if not data.bars.contains_key(self.symbols[0]):
            return
        self.sessions += 1
        if self.sessions == 2:
            self.record("train_requested", {"at_utc": str(self.utc_time)})
            self.train(self._slow_work)
        elif self.sessions == 3:
            self.earnings_typed_variants()
        elif self.sessions == 4:
            self.earnings_typed_full_span()

    def on_warmup_finished(self):
        self.record("warmup", {"seconds": round(wall.perf_counter() - self.started, 1),
                               "callbacks": self.warm_calls, "bars": self.warm_bars,
                               "listings": self.warm_listings, "first_time": self.first_warm_time,
                               "finished_at": self.time.isoformat()})

    def earnings_typed_variants(self):
        """One year of the calendar in the typed shapes, each counted and timed."""
        if self.earnings is None:
            self.earnings = self.add_data(EODHDUpcomingEarnings, "earnings").symbol
        start, stop = datetime(2020, 1, 1), datetime(2021, 1, 1)
        out = {}

        def timed(name, read):
            began = wall.perf_counter()
            try:
                count = read()
                out[name] = {"rows": count, "seconds": round(wall.perf_counter() - began, 2)}
            except Exception as error:  # noqa: BLE001  (a failed variant is a result)
                out[name] = {"error": f"{type(error).__name__}: {error}"[:300],
                             "seconds": round(wall.perf_counter() - began, 2)}

        timed("typed_symbol_range", lambda: sum(
            1 for _ in self.history[EODHDUpcomingEarnings](self.earnings, start, stop)))
        timed("typed_symbol_range_daily", lambda: sum(
            1 for _ in self.history[EODHDUpcomingEarnings](self.earnings, start, stop,
                                                           Resolution.DAILY)))
        timed("typed_list_range_daily", lambda: sum(
            len(items) for items in self.history[EODHDUpcomingEarnings](
                [self.earnings], start, stop, Resolution.DAILY)))
        timed("typed_symbol_span_daily", lambda: sum(
            1 for _ in self.history[EODHDUpcomingEarnings](self.earnings, timedelta(days=366),
                                                           Resolution.DAILY)))
        timed("slices_range_daily", lambda: sum(
            slice.get(EODHDUpcomingEarnings).count
            for slice in self.history(start, stop, Resolution.DAILY)))
        timed("dataframe_range", lambda: int(len(
            self.history(EODHDUpcomingEarnings, self.earnings, start, stop))))
        self.record("earnings_typed_variants", out)

    def earnings_typed_full_span(self):
        """The whole 6,800-day calendar in the typed shape that returns rows (a list of
        symbols with a daily resolution), a year at a time as the adapter reads it, keeping
        only the rows of the panel's securities: the realistic consumer cost."""
        if self.earnings is None:
            self.earnings = self.add_data(EODHDUpcomingEarnings, "earnings").symbol
        panel = set(self.symbols)
        end = self.time
        year = end - timedelta(days=6800)
        began = wall.perf_counter()
        per_year, rows, kept = [], 0, 0
        while year < end:
            stop = min(end, datetime(year.year + 1, 1, 1))
            t = wall.perf_counter()
            count = mine = 0
            for items in self.history[EODHDUpcomingEarnings]([self.earnings], year, stop,
                                                             Resolution.DAILY):
                for item in items.values():
                    count += 1
                    if item.symbol in panel:
                        mine += 1
                        (item.report_date, item.report_time)
            per_year.append([year.year, count, mine, round(wall.perf_counter() - t, 2)])
            rows += count
            kept += mine
            year = stop
        self.record("earnings_typed_full_span", {"per_year": per_year, "rows": rows,
                                                 "panel_rows": kept,
                                                 "seconds": round(wall.perf_counter() - began, 2)})

    def _slow_work(self):
        began = wall.perf_counter()
        self.record("train_started", {"at_utc": str(self.utc_time), "at": self.time.isoformat()})
        wall.sleep(TRAIN_SLEEP)
        self.record("train_finished", {"seconds": round(wall.perf_counter() - began, 1),
                                       "at_utc": str(self.utc_time)})

    def on_end_of_algorithm(self):
        self.record("summary", {"sessions_after_warmup": self.sessions,
                                "total_seconds": round(wall.perf_counter() - self.started, 1)})

    def record(self, name, payload):
        payload = {"task": name, "at": self.time.isoformat(), **payload}
        self.log("NATIVE_DIAG " + json.dumps(payload, default=str))
