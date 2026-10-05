"""Run `python -m examples.technical.memory [ema|rsi|donchian]`.

Synthetic daily sessions and immediate explicit fills illustrate plumbing only.
There are no realistic fees, spreads, exchange calendar, or performance claims.
"""

# Snapshot replacement updates marks/NAV while retaining immutable account fields.
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# Array construction creates a deliberate synthetic decline/rally/decline exercise.
import numpy as np

# The memory boundary makes data, sessions and fills explicit inputs to the demonstration.
from sugarloaf.adapters.memory.gateway import MemoryGateway
from sugarloaf.adapters.memory.services import MemoryServices
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.services import AccountSnapshot
from sugarloaf.core.types import InstrumentId
from sugarloaf.execution import ExecutionEngine, TradingSession
from sugarloaf.market.contracts import Bar, Instrument

from .nodes import DonchianBreakout, EMACrossover, RSIMeanReversion, build_graph


def run(strategy="ema"):
    # A provider-neutral identity permits this entire fixture to run without QC imports.
    instrument = InstrumentId("fixture:DEMO")
    constructors = {"ema": EMACrossover, "rsi": RSIMeanReversion, "donchian": DonchianBreakout}
    # A fresh node resets hysteresis/strategy state for each invocation.
    node = constructors[strategy]((instrument,), weight=0.2)
    start = datetime(2025, 1, 1, 16, tzinfo=timezone.utc)
    step = timedelta(days=1)
    # Begin with enough flat observations to satisfy the chosen node's warmup requirement.
    # Subsequent moves expose trend entry/exit and the contrarian rule's last decline entry.
    prices = np.r_[
        np.full(node.bars.lookback, 100.0),
        np.linspace(99, 80, 25),
        np.linspace(81, 120, 50),
        np.linspace(119, 85, 35),
    ]
    times = [start + k * step for k in range(len(prices))]
    services = MemoryServices(
        (Instrument(instrument, "DEMO"),),
        # USD100,000 starts in cash. Marks are prices per unit, not returns.
        AccountSnapshot({}, 100000, {"USD": 100000}, {instrument: 100}),
        # Invent one-hour daily sessions, including calendar days absent from real exchanges.
        calendars={instrument: {"regular": tuple((t, t + timedelta(hours=1)) for t in times)}},
    )
    gateway = MemoryGateway(services)
    execution = ExecutionEngine(gateway)
    # Fill callbacks keep the engine ledger synchronized with the fixture account.
    gateway.on_update = execution.on_order_update
    session = TradingSession(build_graph(node), services, execution)
    runtime = session.runtime
    bars, transitions = [], []
    previous_target = None
    replay_added = 0
    for k, (now, price) in enumerate(zip(times, prices), start=1):
        price = float(price)
        # Observation end and receipt both equal now: this fixture assumes immediate data.
        # Small high/low offsets give Donchian channels a nonzero width around the close.
        bars.append(
            Bar(instrument, now - step, now, now, price, price + 0.1, price - 0.1, price, 1000)
        )
        services.put_bars(node.bars, (bars[-1],))
        # Mark holdings at the current fixture price before evaluating a NAV-weight target.
        account = services.account
        services.set_account(
            replace(
                account,
                prices={instrument: price},
                nav=account.cash["USD"] + account.holdings.get(instrument, 0) * price,
            )
        )
        # A strictly increasing sequence identifies one admitted completed-bar boundary.
        batch = EventBatch(k, now, (Event("bar_close", instrument, interval=step),))
        session.process(batch)
        views = runtime.store.get(*runtime.graph.output)
        # Before history is ready, the graph may have no target; missing output is not an exit.
        if views:
            weight = views[0].positions[instrument]
            if weight != previous_target:
                # Record regime transitions separately from weight-maintenance orders.
                transitions.append({"at": now.isoformat(), "target_weight": weight})
                previous_target = weight
        for order_id, order in tuple(gateway.orders.items()):
            if order.status == "open" and order.remaining:
                # The caller fills every remaining unit at the mark; there is no fill model.
                gateway.fill(order_id, order.remaining, price)
        before = len(gateway.orders)
        # Replaying the identical boundary must not generate another target/order issuance.
        session.process(batch)
        replay_added += len(gateway.orders) - before
    return {
        # These outputs verify wiring, state and replay, not realistic investment returns.
        "strategy": strategy,
        "transitions": transitions,
        "orders": len(gateway.orders),
        "replay_added_orders": replay_added,
        "final_quantity": services.account.holdings.get(instrument, 0),
        "fixture_only": True,
    }


if __name__ == "__main__":
    import argparse
    import json

    # Keep the small fixture runnable as a module without requiring a cloud account.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", choices=("ema", "rsi", "donchian"), nargs="?", default="ema")
    print(json.dumps(run(parser.parse_args().strategy), indent=2))
