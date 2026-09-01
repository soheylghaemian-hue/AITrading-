from atp.core.enums import AssetClass
from atp.instruments.global_catalog import (
    CatalogueStatus,
    GlobalContract,
    GlobalInstrumentCatalogue,
)


def contract(con_id=1, **changes):
    values = {
        "con_id": con_id,
        "symbol": "SAP",
        "local_symbol": "SAP",
        "sec_type": "STK",
        "exchange": "SMART",
        "primary_exchange": "IBIS",
        "currency": "EUR",
        "country": "DE",
    }
    values.update(changes)
    return GlobalContract(**values)


def test_ingest_deduplicates_by_stable_contract_id():
    catalogue = GlobalInstrumentCatalogue()
    assert catalogue.ingest([contract(), contract()]) == 1
    snapshot = catalogue.snapshot()
    assert len(snapshot.contracts) == 1
    assert snapshot.contracts[0].status is CatalogueStatus.RESOLVED


def test_ready_requires_trading_and_live_or_delayed_data_permission():
    catalogue = GlobalInstrumentCatalogue()
    catalogue.ingest([contract()])

    blocked = catalogue.set_permissions(
        1, trading_allowed=True, market_data_allowed=False, delayed_data_allowed=False
    )
    assert blocked.status is CatalogueStatus.SUBSCRIPTION_REQUIRED
    assert catalogue.ready() == []

    ready = catalogue.set_permissions(
        1, trading_allowed=True, market_data_allowed=False, delayed_data_allowed=True
    )
    assert ready.status is CatalogueStatus.READY
    assert catalogue.ready(AssetClass.EQUITY) == [ready]


def test_trading_permission_is_a_separate_fail_closed_gate():
    catalogue = GlobalInstrumentCatalogue()
    catalogue.ingest([contract()])
    item = catalogue.set_permissions(1, trading_allowed=False, market_data_allowed=True)
    assert item.status is CatalogueStatus.TRADING_PERMISSION_REQUIRED


def test_invalid_and_incomplete_contracts_are_rejected():
    catalogue = GlobalInstrumentCatalogue()
    catalogue.ingest(
        [
            contract(2, sec_type="UNKNOWN"),
            contract(3, sec_type="FUT", expiry=""),
            contract(4, exchange=""),
        ]
    )
    assert catalogue.snapshot().counts[CatalogueStatus.REJECTED.value] == 3


def test_multi_asset_contracts_are_classified():
    catalogue = GlobalInstrumentCatalogue()
    catalogue.ingest(
        [
            contract(10, symbol="EUR", sec_type="CASH", exchange="IDEALPRO", currency="USD"),
            contract(11, symbol="ES", sec_type="FUT", exchange="CME", expiry="202612"),
            contract(12, symbol="AAPL", sec_type="OPT", exchange="SMART", expiry="20261016"),
            contract(13, symbol="BTC", sec_type="CRYPTO", exchange="PAXOS", currency="USD"),
            contract(14, symbol="US-T", sec_type="BOND", exchange="SMART", currency="USD"),
            contract(15, symbol="BAS", sec_type="WAR", exchange="FWB", currency="EUR"),
            contract(16, symbol="FUND", sec_type="FUND", exchange="FUNDSERV", currency="EUR"),
        ]
    )
    classes = {item.asset_class for item in catalogue.snapshot().contracts}
    assert classes == {
        AssetClass.FX,
        AssetClass.FUTURE,
        AssetClass.OPTION,
        AssetClass.CRYPTO,
        AssetClass.BOND,
        AssetClass.WARRANT,
        AssetClass.FUND,
    }
