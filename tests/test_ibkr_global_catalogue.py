from types import SimpleNamespace

from atp.core.enums import AssetClass
from atp.instruments.ibkr_catalog import IBKRContractQualifier, contract_detail_to_global


def detail(con_id=100, sec_type="STK", symbol="SAP", expiry=""):
    return SimpleNamespace(
        contract=SimpleNamespace(
            conId=con_id,
            symbol=symbol,
            localSymbol=symbol,
            secType=sec_type,
            exchange="SMART",
            primaryExchange="IBIS",
            currency="EUR",
            lastTradeDateOrContractMonth=expiry,
            strike=0,
            right="",
            multiplier="1",
            underConId=0,
        ),
        longName="SAP SE",
        minTick=0.01,
        marketRuleIds="26",
        stockType="",
    )


def test_maps_ibkr_contract_detail_to_stable_global_record():
    item = contract_detail_to_global(detail())
    assert item.con_id == 100
    assert item.asset_class is AssetClass.EQUITY
    assert item.primary_exchange == "IBIS"
    assert item.min_tick == 0.01


def test_maps_ibkr_etf_without_misclassifying_it_as_common_stock():
    source = detail(symbol="SPY")
    source.stockType = "ETF"
    assert contract_detail_to_global(source).asset_class is AssetClass.ETF


class FakeIB:
    async def reqContractDetailsAsync(self, candidate):
        if candidate.symbol == "MISSING":
            return []
        return [detail(candidate.conId, candidate.secType, candidate.symbol, candidate.expiry)]


async def test_qualifier_resolves_batches_and_reports_missing_contracts():
    candidates = [
        SimpleNamespace(conId=1, secType="STK", symbol="SAP", exchange="SMART", expiry=""),
        SimpleNamespace(conId=2, secType="FUT", symbol="ES", exchange="CME", expiry="202612"),
        SimpleNamespace(conId=3, secType="STK", symbol="MISSING", exchange="SMART", expiry=""),
    ]
    result = await IBKRContractQualifier(FakeIB(), batch_size=2).qualify(candidates)
    assert result.requested == 3
    assert {item.con_id for item in result.resolved} == {1, 2}
    assert result.unresolved == ("STK:MISSING@SMART",)
