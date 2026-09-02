"""§ WP9 — official-directory adapters: FIRDS/SEC parsing, CFI mapping, cursor stub, grouping (adversarial).

Covers no-fabrication (unmappable/incomplete records are skipped, never guessed), global identifiers (ISIN +
venue MIC read verbatim), derivative identity (expiry/strike/right/underlying) and cursor resumability.
"""
from __future__ import annotations

from atp.instruments.directories import (
    DirectoryPage,
    StubDirectoryProvider,
    cfi_to_sec_type,
    directory_to_provider,
    firds_market_plan,
    firds_market_sources,
    parse_firds_fulins,
    parse_sec_company_tickers,
)
from atp.instruments.listing_sources import ListingCandidate

_FIRDS = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:auth.049.001.02">
 <FinInstrmRptgRefDataRpt>
  <RefData>
   <FinInstrmGnlAttrbts><Id>FR0000131104</Id><FullNm>BNP PARIBAS</FullNm>
     <ClssfctnTp>ESVUFR</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XPAR</Id></TradgVnRltdAttrbts>
  </RefData>
  <RefData>
   <FinInstrmGnlAttrbts><Id>DE000TESTOPT1</Id><FullNm>OPT ON DAX</FullNm>
     <ClssfctnTp>OCASPS</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XEUR</Id></TradgVnRltdAttrbts>
   <DerivInstrmAttrbts><XpryDt>2026-12-18</XpryDt><PricMltplr>5</PricMltplr><OptnTp>CALL</OptnTp>
     <StrkPric><Pric><Amt>20000</Amt></Pric></StrkPric>
     <UndrlygInstrm><Sngl><ISIN>DE0008469008</ISIN></Sngl></UndrlygInstrm></DerivInstrmAttrbts>
  </RefData>
  <RefData>
   <FinInstrmGnlAttrbts><Id>NOCLASS</Id><ClssfctnTp>ZZZZZZ</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
   <TradgVnRltdAttrbts><Id>XPAR</Id></TradgVnRltdAttrbts>
  </RefData>
  <RefData>
   <FinInstrmGnlAttrbts><Id>NOMIC1234567</Id><ClssfctnTp>ESVUFR</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
  </RefData>
 </FinInstrmRptgRefDataRpt>
</Document>"""


def test_cfi_mapping_is_conservative():
    assert cfi_to_sec_type("ESVUFR") == "STK"
    assert cfi_to_sec_type("CEOIXX") == "ETF"       # C + E group = exchange-traded fund
    assert cfi_to_sec_type("CIOIXX") == "FUND"      # other CIVs = fund
    assert cfi_to_sec_type("DBFUFR") == "BOND"
    assert cfi_to_sec_type("OCASPS") == "OPT"
    assert cfi_to_sec_type("FFICSX") == "FUT"
    assert cfi_to_sec_type("RWSXXX") == "WAR"
    assert cfi_to_sec_type("ZZZZZZ") is None        # unknown category → skip, never guessed
    assert cfi_to_sec_type("") is None and cfi_to_sec_type(None) is None


def test_firds_parses_equity_and_derivative_and_skips_incomplete():
    cands = parse_firds_fulins(_FIRDS)
    # NOCLASS (unmappable CFI) and NOMIC (no venue) are skipped — never fabricated
    assert [c.symbol for c in cands] == ["FR0000131104", "DE000TESTOPT1"]
    eq = cands[0]
    assert eq.sec_type == "STK" and eq.exchange == "XPAR" and eq.currency == "EUR"
    assert eq.isin == "FR0000131104" and eq.expiry is None and eq.option_right is None
    opt = cands[1]
    assert opt.sec_type == "OPT" and opt.exchange == "XEUR"       # venue MIC, never SMART
    assert opt.expiry == "2026-12-18" and opt.strike == "20000" and opt.option_right == "C"
    assert opt.underlying_symbol == "DE0008469008" and opt.multiplier == "5"


def test_firds_venue_is_a_real_mic_never_smart():
    for c in parse_firds_fulins(_FIRDS):
        assert c.exchange and c.exchange.upper() not in ("SMART", "SMARTUS", "")


def test_firds_market_sources_group_by_venue():
    markets = firds_market_sources(parse_firds_fulins(_FIRDS))
    ids = sorted(m.plan.market_id for m in markets)
    assert ids == ["XEUR", "XPAR"]
    xpar = next(m for m in markets if m.plan.market_id == "XPAR")
    assert xpar.plan.region == "EUROPE" and xpar.plan.country == "FR"       # documented MIC facts
    assert [c.symbol for c in xpar.provider()] == ["FR0000131104"]


def test_firds_unknown_mic_keeps_venue_but_country_is_null():
    plan = firds_market_plan("XZZZ")
    assert plan.market_id == "XZZZ" and plan.region == "EUROPE" and plan.country == ""   # NO DATA, not guessed


def test_sec_parses_exchange_variant_and_skips_venueless():
    text = ('{"fields":["cik","name","ticker","exchange"],'
            '"data":[[320193,"Apple Inc.","AAPL","Nasdaq"],[789019,"Microsoft","MSFT","Nasdaq"],'
            '[1,"NoVenue","NV",""],[2,"NoTicker","","NYSE"]]}')
    cands = parse_sec_company_tickers(text)
    assert {(c.symbol, c.exchange) for c in cands} == {("AAPL", "NASDAQ"), ("MSFT", "NASDAQ")}
    assert all(c.sec_type == "STK" and c.currency == "USD" for c in cands)


def test_stub_directory_provider_paginates_by_cursor():
    p = StubDirectoryProvider(pages=[[ListingCandidate("A", "STK", "XNAS", "USD", "")],
                                     [ListingCandidate("B", "STK", "XNAS", "USD", "")]])
    page0 = p.fetch_candidates()
    assert isinstance(page0, DirectoryPage) and [c.symbol for c in page0.candidates] == ["A"]
    assert page0.next_cursor == "1"
    page1 = p.fetch_candidates(cursor="1")
    assert [c.symbol for c in page1.candidates] == ["B"] and page1.next_cursor is None
    # bridge drains all pages into a single ListingProvider (stub is re-callable / stateless per cursor)
    provider = directory_to_provider(p)
    assert [c.symbol for c in provider()] == ["A", "B"]


def test_directory_to_provider_drains_all_pages():
    p = StubDirectoryProvider(pages=[[ListingCandidate("A", "STK", "XNAS", "USD", "")],
                                     [ListingCandidate("B", "STK", "XNAS", "USD", "")],
                                     [ListingCandidate("C", "STK", "XNAS", "USD", "")]])
    got = [c.symbol for c in directory_to_provider(p)()]
    assert got == ["A", "B", "C"]


def test_deduplicate_keeps_distinct_derivatives():
    # regression: two option strikes sharing (symbol, exchange, sec_type, currency) must NOT collapse
    from atp.instruments.listing_sources import deduplicate_listings
    a = ListingCandidate("OPTROOT", "OPT", "XEUR", "EUR", "", expiry="2026-12-18", strike="100",
                         option_right="C")
    b = ListingCandidate("OPTROOT", "OPT", "XEUR", "EUR", "", expiry="2026-12-18", strike="200",
                         option_right="C")
    out = deduplicate_listings([a, b, a])
    assert len(out) == 2 and sorted(o.strike for o in out) == ["100", "200"]
    # cash instruments (no derivative fields) still collapse on the base key
    c = ListingCandidate("AAPL", "STK", "NASDAQ", "USD", "")
    assert len(deduplicate_listings([c, c])) == 1


def test_firds_skips_record_without_currency():
    # currency is part of the natural key → a FIRDS record without NtnlCcy is skipped, never defaulted
    xml = ("""<?xml version="1.0"?><Document xmlns="urn:x">
      <RefData><FinInstrmGnlAttrbts><Id>GB00TESTNOCCY0</Id><ClssfctnTp>ESVUFR</ClssfctnTp>"""
           """</FinInstrmGnlAttrbts><TradgVnRltdAttrbts><Id>XLON</Id></TradgVnRltdAttrbts></RefData>
      </Document>""")
    assert parse_firds_fulins(xml) == []


def test_unavailable_stub_raises():
    import pytest

    from atp.instruments.directories import DirectoryUnavailableError
    p = StubDirectoryProvider(pages=[], unavailable=True)
    with pytest.raises(DirectoryUnavailableError):
        p.fetch_candidates()
