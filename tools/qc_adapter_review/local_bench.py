"""Local timings of the adapter's own Python for a 4,900-bar daily window.

Runs on this machine against the tests' fake LEAN boundary, so Python.NET costs are not
included; what it measures is the adapter's data model: admitting a new bar (and its
dedupe scan of the whole window), one read pass as ``_ContextServices._read`` filters a
window, and building one neutral ``Bar`` as ``_fill_window`` does. Scaled to the 214
securities of the move-size panel for comparison with the QuantConnect probe.

    uv run --cache-dir .uv-cache python tools/qc_adapter_review/local_bench.py
"""

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from test_adapter_quantconnect import API, algorithm  # noqa: E402  (the tests' fake boundary)

from sugarloaf.adapters.base import resource_key  # noqa: E402
from sugarloaf.adapters.quantconnect import QuantConnectServices  # noqa: E402
from sugarloaf.core.events import EventBatch  # noqa: E402
from sugarloaf.core.primitives import InstrumentId  # noqa: E402
from sugarloaf.market.contracts import Bar, BarsRequirement, ResourceUpdate, Selection  # noqa: E402

NOW = datetime(2021, 1, 4, 21, 0, tzinfo=UTC)
DAY = timedelta(days=1)
MEMBERS, LOOKBACK, PANEL_BARS = 214, 4900, 760_000


def main():
    api, platform = API(), algorithm()
    platform.utc_time = NOW
    services = QuantConnectServices(platform, api=api)
    instrument = InstrumentId("USA:A")
    services.register(instrument, "A")
    req = BarsRequirement(Selection((instrument,)), DAY, lookback=LOOKBACK,
                          normalization="adjusted")
    services.install("n", ResourceUpdate((req,), ()))
    services.apply(EventBatch(0, NOW))  # subscribes and creates the window
    key = resource_key(req, instrument)
    window = services.windows[key]
    window.clear()
    for k in range(LOOKBACK, 0, -1):
        end = NOW - k * DAY
        window.append(Bar(instrument, end - DAY, end, end, 100.0, 101.0, 99.0, 100.0, 1000.0,
                          False))

    def native(end):
        return NS(time=end - DAY, end_time=end, open=100.0, high=101.0, low=99.0, close=100.0,
                  volume=1000.0, is_fill_forward=False)

    # Admission of a new bar: the normal streaming case, one per member per session.
    began = time.perf_counter()
    for k in range(1, MEMBERS + 1):
        end = NOW + k * DAY
        services._admit_bar(key, native(end), end, indicators=False)
    admit = (time.perf_counter() - began) / MEMBERS
    print(f"_admit_bar, new bar into a {LOOKBACK}-bar window: {admit * 1e3:.2f} ms per bar; "
          f"{admit * MEMBERS:.2f} s a session for {MEMBERS} members")

    # The dedupe scan on its own: the search for an equal stamp over the whole window.
    stamp = NOW + 5000 * DAY
    began = time.perf_counter()
    for _ in range(50):
        next((i for i, item in enumerate(window) if services._stamp(item) == stamp), None)
    scan = (time.perf_counter() - began) / 50
    print(f"dedupe scan alone: {scan * 1e3:.2f} ms per bar; {scan * MEMBERS:.2f} s a session")

    # One read pass, as _ContextServices._read filters a window for a Context.
    now = available = NOW + 300 * DAY
    values = tuple(window)
    began = time.perf_counter()
    for _ in range(20):
        tuple(x for x in values
              if x.available_at <= available and x.end <= now
              and (req.fill_forward or not x.fill_forward))
    read = (time.perf_counter() - began) / 20
    print(f"_read filter pass over {len(values)} bars: {read * 1e3:.2f} ms; "
          f"{read * MEMBERS * 2:.2f} s for two passes over {MEMBERS} members")

    # Building one neutral Bar as _fill_window does, with the fake (cheap) to_utc.
    security = platform.securities[services.symbols[instrument]]
    began = time.perf_counter()
    for k in range(20000):
        end = NOW + k * DAY
        Bar(instrument, api.to_utc(security, end - DAY), api.to_utc(security, end), end,
            100.0, 101.0, 99.0, 100.0, 1000.0, False)
    build = (time.perf_counter() - began) / 20000
    print(f"neutral Bar construction: {build * 1e6:.1f} us per bar; "
          f"{build * PANEL_BARS:.1f} s for {PANEL_BARS:,} bars")
    print("python", sys.version.split()[0])


if __name__ == "__main__":
    main()
