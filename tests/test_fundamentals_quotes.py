"""Generic fundamental fields (catalogue, declaration, projection) and quote normalization."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from sugarloaf.adapters.base import resource_key
from sugarloaf.adapters.memory import MemoryServices
from sugarloaf.adapters.quantconnect import QuantConnectServices
from sugarloaf.adapters.quantconnect import fundamental_catalogue as catalogue
from sugarloaf.adapters.quantconnect.lean import LeanAPI
from sugarloaf.adapters.quantconnect.services import _fundamental_record
from sugarloaf.core.errors import CapabilityError, ContractError
from sugarloaf.core.events import Event, EventBatch
from sugarloaf.core.primitives import InstrumentId
from sugarloaf.market.contracts import (
    FundamentalRecord,
    FundamentalsRequirement,
    FundamentalUniverse,
    QuotesRequirement,
    ResourceUpdate,
    Selection,
    UniverseRequirement,
    field_names,
)

NOW = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
A, B = InstrumentId("USA:A"), InstrumentId("USA:B")
PE = "morningstar.valuation_ratios.pe_ratio"
ROE = "morningstar.operation_ratios.roe.one_year"
SECTOR = "morningstar.asset_classification.morningstar_sector_code"


def test_catalogue_names_types_and_explicit_periods():
    assert catalogue.VERSION.startswith("lean-")
    assert catalogue.resolve(SECTOR) == (("asset_classification", "morningstar_sector_code"),
                                         "int")
    assert catalogue.resolve("morningstar.company_reference.is_reit")[1] == "bool"
    assert catalogue.resolve(ROE) == (("operation_ratios", "roe", "one_year"), "float")
    # A multi-period value is read only with its period named; LEAN's default period and
    # its has_value flag are not periods.
    assert catalogue.resolve("morningstar.operation_ratios.roe") is None
    assert catalogue.resolve("morningstar.operation_ratios.roe.value") is None
    assert catalogue.resolve("morningstar.operation_ratios.roe.has_value") is None
    assert catalogue.resolve("morningstar.no_such.field") is None
    assert all(name.startswith("morningstar.") for name in catalogue.FIELDS)
    assert {kind for kind, _ in catalogue.FIELDS.values()} <= {"float", "int", "bool", "str",
                                                              "date"}


def test_record_fields_are_a_validated_read_only_copy():
    source = {PE: 0.0, "x.flag": False, "x.when": date(2024, 1, 2), "x.missing": None}
    record = FundamentalRecord(A, NOW, morningstar_sector_code=311, is_reit=False, fields=source)
    source[PE] = 99.0
    # A valid zero or False is kept; missing is None; the source dict cannot change it.
    assert record.value(PE) == 0.0 and record.value("x.flag") is False
    assert record.value("x.missing") is None
    with pytest.raises(TypeError):
        record.fields[PE] = 1.0
    with pytest.raises(CapabilityError, match="Undeclared"):
        record.value(ROE)
    with pytest.raises(ContractError):
        FundamentalRecord(A, NOW, fields={PE: float("nan")})
    with pytest.raises(ContractError):
        FundamentalRecord(A, NOW, fields={PE: [1.0]})
    with pytest.raises(ContractError):
        FundamentalRecord(A, NOW, morningstar_sector_code=True)
    # Projection keeps exactly the declared names; one the record lacks is None.
    projected = record.project([ROE, PE])
    assert dict(projected.fields) == {PE: 0.0, ROE: None}
    assert projected.morningstar_sector_code == 311
    hash(record)


def test_declared_fields_are_canonical_and_part_of_equality():
    assert field_names([PE, ROE, PE]) == (ROE, PE)
    with pytest.raises(ContractError):
        field_names(PE)
    with pytest.raises(ContractError):
        field_names([""])
    selection = Selection(universe="u")
    assert (FundamentalsRequirement(selection, fields=[ROE, PE])
            == FundamentalsRequirement(selection, fields=(PE, ROE)))
    rule = lambda at, rows: []  # noqa: E731
    assert FundamentalUniverse("u", rule, fields=[PE]) != FundamentalUniverse("u", rule)


def test_memory_reads_only_declared_fields_and_rejects_unknown_names():
    record = FundamentalRecord(A, NOW - timedelta(hours=1), "1", fields={PE: 12.5, ROE: 0.2})
    services = MemoryServices(fundamentals={A: [record]})
    reader = FundamentalsRequirement(Selection((A,)), fields=[PE])
    other = FundamentalsRequirement(Selection((A,)), fields=[ROE, PE])
    services.install("n", ResourceUpdate((reader, other), ()))
    services.apply(EventBatch(0, NOW))
    ctx = services.context(node_id="n")
    # Two readers of one security each get their own declared fields.
    assert dict(ctx.market.fundamentals(reader, A).fields) == {PE: 12.5}
    assert dict(ctx.market.fundamentals(other, A).fields) == {PE: 12.5, ROE: 0.2}
    with pytest.raises(CapabilityError, match="Undeclared"):
        ctx.market.fundamentals(reader, A).value(ROE)
    unknown = FundamentalsRequirement(Selection((A,)), fields=["morningstar.nope"])
    with pytest.raises(CapabilityError, match="Unsupported fundamental fields"):
        services.install("m", ResourceUpdate((unknown,), ()))


def test_universe_rule_sees_its_declared_fields_and_conflicts_are_rejected():
    seen = []

    def rule(at, rows):
        seen.extend(dict(r.fields) for r in rows)
        return [r.instrument for r in rows if r.value(PE) is not None and r.value(PE) < 20]

    services = MemoryServices(fundamental_fields=[PE, ROE])
    definition = FundamentalUniverse("value", rule, fields=[PE])
    services.install("n", ResourceUpdate((UniverseRequirement(definition),), ()))
    rows = [FundamentalRecord(A, NOW, fields={PE: 10.0, ROE: 0.1}),
            FundamentalRecord(B, NOW, fields={})]
    services.set_selection("value", rows, NOW)
    services.apply(EventBatch(0, NOW, (Event("universe"),)))
    assert services.memberships["value"].members == (A,)
    # The rule saw only PE, and None where a row did not have it.
    assert seen == [{PE: 10.0}, {PE: None}]
    clash = FundamentalUniverse("value", rule, fields=[PE, ROE])
    with pytest.raises(ContractError):
        services.install("m", ResourceUpdate((UniverseRequirement(clash),), ()))


def test_qc_record_reads_declared_fields_with_explicit_conversions():
    roe = NS(one_year=0.0, three_months=float("nan"))
    item = NS(
        company_reference=NS(cik="0000320193", is_reit=False, legal_name=" ",
                             expected_fiscal_year_end=datetime(2025, 9, 30)),
        asset_classification=NS(morningstar_sector_code=311),
        security_reference=NS(ipo_date=NS(Year=1, Month=1, Day=1)),
        operation_ratios=NS(roe=roe),
        valuation_ratios=NS(pe_ratio=float("nan")),
        market_cap=2.7e12, price=180.0, dollar_volume=1e9,
    )
    names = (ROE, "morningstar.operation_ratios.roe.three_months", PE,
             "morningstar.company_reference.legal_name",
             "morningstar.company_reference.expected_fiscal_year_end",
             "morningstar.security_reference.ipo_date")
    record = _fundamental_record(A, item, NOW, names)
    assert record.morningstar_sector_code == 311 and record.is_reit is False
    assert record.value(ROE) == 0.0
    assert record.value("morningstar.operation_ratios.roe.three_months") is None
    assert record.value(PE) is None
    assert record.value("morningstar.company_reference.legal_name") is None
    assert record.value("morningstar.company_reference.expected_fiscal_year_end") == date(2025, 9,
                                                                                          30)
    assert record.value("morningstar.security_reference.ipo_date") is None
    # LEAN's 0 sector means none.
    item.asset_classification.morningstar_sector_code = 0
    assert _fundamental_record(A, item, NOW).morningstar_sector_code is None


def test_qc_install_rejects_unknown_fields():
    from test_adapter_quantconnect import API, algorithm

    services = QuantConnectServices(algorithm(), api=API())
    bad = FundamentalsRequirement(Selection((A,)), fields=["morningstar.operation_ratios.roe"])
    with pytest.raises(CapabilityError, match="Unsupported fundamental fields"):
        services.install("n", ResourceUpdate((bad,), ()))


def test_quote_normalization_is_explicit_and_used_for_history_and_universe_feeds():
    raw = QuotesRequirement(Selection((A,)))
    adjusted = QuotesRequirement(Selection((A,)), normalization="adjusted")
    assert raw.normalization == "raw"
    with pytest.raises(ContractError):
        QuotesRequirement(Selection((A,)), normalization="dividend")
    # The two scales are different backing resources.
    assert resource_key(raw, A) != resource_key(adjusted, A)
    calls = []

    class History:
        def __getitem__(self, cls):
            return lambda *args, **kwargs: calls.append(kwargs["data_normalization_mode"])

    api = object.__new__(LeanAPI)
    api.lean = NS(QuoteBar="QuoteBar", TradeBar="TradeBar",
                  DataNormalizationMode=NS(RAW="RAW", ADJUSTED="ADJUSTED"),
                  Extensions=NS(convert_from_utc=lambda t, zone: t),
                  Resolution=NS(MINUTE="MINUTE", DAILY="DAILY", HOUR="HOUR"))
    platform = NS(history=History(), time_zone="America/New_York")
    api.history(platform, "A", adjusted, NOW, 10)
    api.history(platform, "A", raw, NOW, 10)
    assert calls == ["ADJUSTED", "RAW"]


def test_memory_quotes_are_kept_apart_by_normalization():
    from sugarloaf.core.errors import NotReady
    from sugarloaf.market.contracts import Quote

    raw = QuotesRequirement(Selection((A,)))
    adjusted = QuotesRequirement(Selection((A,)), normalization="adjusted")
    services = MemoryServices()
    services.install("n", ResourceUpdate((raw, adjusted), ()))
    services.put_quotes(raw, [Quote(A, NOW, NOW, 99.0, 101.0)])
    services.apply(EventBatch(0, NOW))
    ctx = services.context(node_id="n")
    assert ctx.market.quotes(raw, A)[-1].bid == 99.0
    with pytest.raises(NotReady):
        ctx.market.quotes(adjusted, A)
