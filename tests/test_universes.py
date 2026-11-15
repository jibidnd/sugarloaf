"""Liquidity selection with and without excluded sectors (sugarloaf.core.universes.select)."""

import pytest

from sugarloaf.core.errors import ContractError
from sugarloaf.core.inputs import LiquidityUniverse
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.core.universes import SelectionRow, select

REIT, BANK, TECH, OTHER = (InstrumentId(f"USA:{t}") for t in ("REIT", "BANK", "TECH", "OTHER"))
# Dollar volume ranks REIT first; its sector is 104 (real estate).
ROWS = (
    SelectionRow(REIT, 50.0, 4e6, 2e8, has_fundamentals=True, sector=104),
    SelectionRow(BANK, 40.0, 4e6, 1.6e8, has_fundamentals=True, sector=103),
    SelectionRow(TECH, 30.0, 4e6, 1.2e8, has_fundamentals=True, sector=311),
    SelectionRow(OTHER, 20.0, 4e6, 8e7, has_fundamentals=True, sector=None),
)


def test_without_an_exclusion_every_sector_ranks():
    assert select(LiquidityUniverse("liquid", 2), ROWS) == (REIT, BANK)


def test_an_excluded_sector_leaves_before_ranking_and_takes_no_place():
    universe = LiquidityUniverse("liquid", 2, exclude_sectors=(104,))
    # The real estate stock is gone and the next stock by dollar volume fills its place.
    assert select(universe, ROWS) == (BANK, TECH)
    assert select(LiquidityUniverse("liquid", 3, exclude_sectors=(103, 104)), ROWS) == (
        TECH, OTHER)


def test_a_security_without_a_sector_code_is_kept():
    universe = LiquidityUniverse("liquid", 4, exclude_sectors=(104,))
    assert OTHER in select(universe, ROWS)


def test_exclusions_compare_as_a_set_of_codes():
    one = LiquidityUniverse("liquid", 2, exclude_sectors=[104, 103, 104])
    assert one.exclude_sectors == (103, 104)
    assert one == LiquidityUniverse("liquid", 2, exclude_sectors=(103, 104))
    assert hash(one) == hash(LiquidityUniverse("liquid", 2, exclude_sectors=(104, 103)))


@pytest.mark.parametrize("value", [("Real Estate",), ("104",), (999,), (True,), "104", 104.0])
def test_a_name_or_an_unknown_code_is_refused(value):
    # A value that matches no sector code would exclude nothing; it fails visibly.
    with pytest.raises(ContractError):
        LiquidityUniverse("liquid", 2, exclude_sectors=value)


def test_a_single_code_is_accepted_as_one_exclusion():
    assert LiquidityUniverse("liquid", 2, exclude_sectors=104).exclude_sectors == (104,)
