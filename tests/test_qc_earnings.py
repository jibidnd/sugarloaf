"""Rules of the QuantConnect adapter's earnings path, without LEAN.

``listed_tickers`` gives the ticker of every equity a ``Custom("earnings")`` declaration
names, so ``QuantConnectAdapter.add_custom`` can register its security id and
``QuantConnectAdapter._custom`` maps its listings to the declared instrument.
"""

import pytest

from sugarloaf.adapters.quantconnect.adapter import Ticker, listed_tickers
from sugarloaf.core.errors import CapabilityError
from sugarloaf.core.inputs import Custom, Selection
from sugarloaf.core.primitives import InstrumentId

SPY, AAPL, VIX = InstrumentId("USA:SPY"), InstrumentId("USA:AAPL"), InstrumentId("USA:VIX")
TICKERS = {SPY: Ticker("SPY"), AAPL: Ticker("AAPL"), VIX: Ticker("VIX", "index")}


def test_each_named_equity_maps_to_its_ticker():
    declaration = Custom("earnings", Selection((SPY, AAPL)))
    assert listed_tickers(declaration, TICKERS) == {SPY: "SPY", AAPL: "AAPL"}


def test_a_declaration_without_named_instruments_registers_nothing():
    assert listed_tickers(Custom("earnings"), TICKERS) == {}
    assert listed_tickers(Custom("earnings", Selection(universe="top")), TICKERS) == {}


def test_an_instrument_without_a_ticker_binding_is_refused_at_setup():
    declaration = Custom("earnings", Selection((InstrumentId("USA:MSFT"),)))
    with pytest.raises(CapabilityError, match="USA:MSFT"):
        listed_tickers(declaration, TICKERS)


def test_an_index_has_no_earnings_listings():
    with pytest.raises(CapabilityError, match="USA:VIX"):
        listed_tickers(Custom("earnings", Selection((VIX,))), TICKERS)
