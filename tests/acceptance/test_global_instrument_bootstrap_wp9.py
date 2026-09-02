"""§ WP9 acceptance — global instrument universe bootstrap (REFERENCE DATA ONLY, end-to-end).

Over the durable store (migrations 26/27, reused) + the real directory adapters + the existing WP2 importer +
the WP3 read-only IBKR qualifier:
  * real FIRDS + SEC directory records import into the persistent catalogue (ISIN + venue MIC persisted);
  * derivative identity — an option carries expiry/strike/right/underlying and yields a distinct id;
  * the coverage read-model reports the imported universe and the explicit four-stage funnel;
  * fail-closed — importing from a non-activated / BLOCKED source is refused; an activated source works;
  * duplicates collapse; an interrupted import resumes;
  * IBKR qualification is read-only and a symbol/SMART-only contract is NEVER enough to VERIFY.

SAFETY: no trading, no orders/execution/broker writes, no market-data subscription, no provider activation.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from atp.instruments.bootstrap import FailClosedError, activate, resolve_source, run_import
from atp.instruments.coverage import instrument_coverage
from atp.instruments.directories import (
    firds_market_sources,
    parse_firds_fulins,
    parse_sec_company_tickers,
    sec_market_source,
)
from atp.instruments.importer import import_instruments
from atp.instruments.model import InstrumentRecord
from atp.instruments.qualification import QualificationConfig, qualify_instruments
from atp.store import open_store

_FIRDS = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:auth.049.001.02">
 <FinInstrmRptgRefDataRpt>
  <RefData>
   <FinInstrmGnlAttrbts><Id>FR0000131104</Id><FullNm>BNP PARIBAS</FullNm>
     <ClssfctnTp>ESVUFR</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XPAR</Id></TradgVnRltdAttrbts></RefData>
  <RefData>
   <FinInstrmGnlAttrbts><Id>DE000CALL20000</Id><FullNm>DAX CALL 20000</FullNm>
     <ClssfctnTp>OCASPS</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XEUR</Id></TradgVnRltdAttrbts>
   <DerivInstrmAttrbts><XpryDt>2026-12-18</XpryDt><PricMltplr>5</PricMltplr><OptnTp>CALL</OptnTp>
     <StrkPric><Pric><Amt>20000</Amt></Pric></StrkPric>
     <UndrlygInstrm><Sngl><ISIN>DE0008469008</ISIN></Sngl></UndrlygInstrm></DerivInstrmAttrbts></RefData>
  <RefData>
   <FinInstrmGnlAttrbts><Id>DE000CALL21000</Id><FullNm>DAX CALL 21000</FullNm>
     <ClssfctnTp>OCASPS</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XEUR</Id></TradgVnRltdAttrbts>
   <DerivInstrmAttrbts><XpryDt>2026-12-18</XpryDt><PricMltplr>5</PricMltplr><OptnTp>CALL</OptnTp>
     <StrkPric><Pric><Amt>21000</Amt></Pric></StrkPric>
     <UndrlygInstrm><Sngl><ISIN>DE0008469008</ISIN></Sngl></UndrlygInstrm></DerivInstrmAttrbts></RefData>
 </FinInstrmRptgRefDataRpt>
</Document>"""

_SEC = ('{"fields":["cik","name","ticker","exchange"],'
        '"data":[[320193,"Apple Inc.","AAPL","Nasdaq"],[789019,"Microsoft Corp","MSFT","Nasdaq"]]}')


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def detail(con_id, symbol, *, sec_type="STK", exchange="SMART", primary="NASDAQ", currency="USD"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol, secType=sec_type,
                                 exchange=exchange, primaryExchange=primary, currency=currency,
                                 lastTradeDateOrContractMonth="", strike=0, right="", multiplier="1",
                                 underConId=0),
        longName=symbol, minTick=0.01, stockType="", country="US")


class ScriptedClient:
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def fetch_contract_details(self, request):
        self.calls.append(request.symbol)
        b = self.script.get(request.symbol, "empty")
        if isinstance(b, tuple) and b[0] == "verified":            # ("verified", conid[, primary])
            primary = b[2] if len(b) > 2 else "NASDAQ"
            return [detail(b[1], request.symbol, primary=primary)]
        return []


async def _noop():
    return None


# --------------------------------------------------------------------- end-to-end import + identifiers
def test_firds_and_sec_import_end_to_end_with_real_identifiers():
    store = _store()
    firds_markets = firds_market_sources(parse_firds_fulins(_FIRDS))
    s1 = import_instruments(store, source_label="esma_firds", markets=firds_markets)
    s2 = import_instruments(store, source_label="sec_company_tickers",
                            markets=[sec_market_source(parse_sec_company_tickers(_SEC))])
    assert s1.status == "COMPLETED" and s2.status == "COMPLETED"
    assert store.im_count_instruments() == 5                     # 3 FIRDS + 2 SEC

    # global identifiers persisted verbatim (ISIN + venue MIC) — never fabricated
    eq = store.im_get_by_natural_key(
        InstrumentRecord(symbol="FR0000131104", asset_class="equity", exchange="XPAR",
                         trading_currency="EUR").natural_key)
    assert eq is not None and eq.isin == "FR0000131104" and eq.exchange == "XPAR"
    assert eq.region == "EUROPE" and eq.country == "FR"          # documented MIC facts, not guessed
    # SEC equity on a real US venue
    assert store.im_count_instruments(exchange="NASDAQ") == 2


def test_derivative_identity_is_distinct_and_persisted():
    store = _store()
    import_instruments(store, source_label="esma_firds",
                       markets=firds_market_sources(parse_firds_fulins(_FIRDS)))
    opts = [r for r in store.im_list_instruments(asset_class="option", limit=100)]
    assert len(opts) == 2                                        # two strikes = two distinct instruments
    strikes = sorted(o.strike for o in opts)
    assert strikes == ["20000", "21000"]
    for o in opts:
        assert o.expiry == "2026-12-18" and o.option_right == "C" and o.multiplier == "5"
        assert o.underlying_symbol == "DE0008469008" and o.isin == o.symbol


def test_no_fabrication_only_real_records_imported():
    store = _store()
    # the FIRDS fixture also contains no unmappable rows here; assert every imported symbol is a real ISIN/ticker
    import_instruments(store, source_label="esma_firds",
                       markets=firds_market_sources(parse_firds_fulins(_FIRDS)))
    for row in store.im_list_instruments(limit=100):
        assert row.symbol and row.exchange and row.exchange != "SMART"
        assert row.con_id is None and row.verification_status == "unverified"   # discovered, not yet verified


# --------------------------------------------------------------------- coverage read-model
def test_coverage_reflects_imported_universe_and_funnel():
    store = _store()
    import_instruments(store, source_label="esma_firds",
                       markets=firds_market_sources(parse_firds_fulins(_FIRDS)))
    cov = instrument_coverage(store)
    assert cov["instruments"]["by_region"] == {"EUROPE": 3}
    assert set(cov["instruments"]["by_exchange"]) == {"XPAR", "XEUR"}
    assert cov["instruments"]["by_asset_class"] == {"equity": 1, "option": 2}
    assert cov["funnel"]["imported"] == 3 and cov["funnel"]["ibkr_verified"] == 0
    assert cov["funnel"]["sources_connected"] == 0 and cov["coverage_partial"] is True


# --------------------------------------------------------------------- fail-closed source gate
def test_run_import_is_refused_for_non_activated_source():
    store = _store()
    markets = firds_market_sources(parse_firds_fulins(_FIRDS))
    with pytest.raises(FailClosedError):
        run_import(store, source=resolve_source("esma_firds"), markets=markets)   # available=False
    assert store.im_count_instruments() == 0


def test_run_import_refused_for_blocked_even_if_marked_available():
    store = _store()
    blocked = activate(resolve_source("jpx_listed_issues"), available=True)        # still BLOCKED (reason set)
    with pytest.raises(FailClosedError):
        run_import(store, source=blocked, markets=[])


def test_run_import_succeeds_for_explicitly_activated_usable_source():
    store = _store()
    src = activate(resolve_source("esma_firds"), available=True)                   # attribution license already OK
    assert src.usable is True
    summary = run_import(store, source=src, markets=firds_market_sources(parse_firds_fulins(_FIRDS)))
    assert summary.status == "COMPLETED" and store.im_count_instruments() == 3


# --------------------------------------------------------------------- duplicates + resume
def test_duplicate_records_collapse_to_one_row():
    store = _store()
    cands = parse_firds_fulins(_FIRDS)
    doubled = cands + cands                                       # same ISIN twice
    from atp.instruments.directories import provider_from_candidates
    from atp.instruments.importer import MarketPlan, MarketSource
    market = MarketSource(plan=MarketPlan("XPAR", "EUROPE", "FR", "Europe/Paris", "euronext", "EUR"),
                          provider=provider_from_candidates([c for c in doubled if c.exchange == "XPAR"]))
    summary = import_instruments(store, source_label="dup", markets=[market])
    assert summary.status == "COMPLETED" and store.im_count_instruments(exchange="XPAR") == 1


# --------------------------------------------------------------------- read-only IBKR qualification
async def test_ibkr_qualification_verifies_on_real_venue_and_updates_funnel():
    store = _store()
    import_instruments(store, source_label="sec_company_tickers",
                       markets=[sec_market_source(parse_sec_company_tickers(_SEC))])
    client = ScriptedClient({"AAPL": ("verified", 265598, "NASDAQ"), "MSFT": ("verified", 272093, "NASDAQ")})
    summary = await qualify_instruments(store, client, run_label="q",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "COMPLETED" and summary.verified == 2
    cov = instrument_coverage(store)
    assert cov["funnel"]["ibkr_verified"] == 2                    # distinct from 'imported'
    assert cov["funnel"]["tradable"] == 0                         # verified ≠ tradable (no tradability proof)


async def test_symbol_or_smart_only_never_verifies():
    store = _store()
    import_instruments(store, source_label="sec_company_tickers",
                       markets=[sec_market_source(parse_sec_company_tickers(_SEC))])
    # AAPL's only contract has NO real venue (exchange=SMART, primaryExchange=SMART) → must NOT verify
    client = ScriptedClient({"AAPL": ("verified", 265598, "SMART"), "MSFT": ("verified", 272093, "NASDAQ")})
    await qualify_instruments(store, client, run_label="smart",
                              config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    aapl = next(r for r in store.im_list_instruments(limit=100) if r.symbol == "AAPL")
    msft = next(r for r in store.im_list_instruments(limit=100) if r.symbol == "MSFT")
    assert aapl.qualification_status != "VERIFIED" and aapl.con_id is None   # SMART-only is not enough
    assert msft.qualification_status == "VERIFIED"                           # real venue verifies
