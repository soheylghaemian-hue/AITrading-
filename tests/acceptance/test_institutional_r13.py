"""Phase R1.3 — Institutional Intelligence Enhancement (read-only, DATA ONLY).

Covers: 13F quarter-over-quarter change analysis (ACCUMULATION / REDUCTION / NEW_POSITION / EXIT), share
change math, accumulation + net-change scores, Form 4 parsing (P→BUY, S→SELL, grants excluded), issuer
Form 4 refs, insider sentiment, collector persistence + immutability, the institutional-flow read-model,
missing data → NO DATA, and no execution / broker / copy-trading. Touches no Trading Core / Risk /
Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atp.institutional.changes import (
    ACCUMULATION, EXIT, NEW_POSITION, REDUCTION, accumulation_score, analyze_changes, net_share_change_pct,
)
from atp.institutional.collector import InstitutionalCollector
from atp.institutional.form4 import parse_form4, parse_issuer_form4_refs
from atp.institutional.insider import insider_sentiment
from atp.institutional.readmodel import build_institutional_flow
from atp.store import open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 14


CUR = [{"symbol": "NVDA", "long_shares": 2000000}, {"symbol": "AAPL", "long_shares": 1000000}]
PREV = [{"symbol": "NVDA", "long_shares": 1000000}, {"symbol": "MSFT", "long_shares": 500000}]

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
    <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>200.50</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-02</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>4000</value></transactionShares>
        <transactionPricePerShare><value>205.00</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-03</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>50000</value></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


# ------------------------------------------------------------------ 13F change analysis
def test_analyze_changes_directions():
    ch = {c["symbol"]: c for c in analyze_changes("FUND A", CUR, PREV, "2026-06-30")}
    assert ch["NVDA"]["direction"] == ACCUMULATION
    assert ch["NVDA"]["share_change"] == 1000000 and ch["NVDA"]["percentage_change"] == 100.0
    assert ch["AAPL"]["direction"] == NEW_POSITION and ch["AAPL"]["percentage_change"] is None
    assert ch["MSFT"]["direction"] == EXIT and ch["MSFT"]["percentage_change"] == -100.0


def test_reduction_and_unchanged():
    ch = analyze_changes("F", [{"symbol": "NVDA", "long_shares": 800}], [{"symbol": "NVDA", "long_shares": 1000}], "p")
    assert ch[0]["direction"] == REDUCTION and ch[0]["percentage_change"] == -20.0
    # unchanged → not a signal
    assert analyze_changes("F", [{"symbol": "NVDA", "long_shares": 1000}], [{"symbol": "NVDA", "long_shares": 1000}], "p") == []


def test_no_previous_is_all_new_positions():
    ch = analyze_changes("F", CUR, None, "p")
    assert all(c["direction"] == NEW_POSITION for c in ch)


def test_accumulation_and_net_scores():
    ch = analyze_changes("FUND A", CUR, PREV, "2026-06-30")   # NVDA acc, AAPL new, MSFT exit
    assert accumulation_score(ch) == round(100.0 * 2 / 3, 1)  # 2 bullish (acc+new) of 3 changing
    assert accumulation_score([]) is None
    assert net_share_change_pct(ch) is not None


# ------------------------------------------------------------------ Form 4 parsing + sentiment
def test_parse_form4_only_open_market():
    p = parse_form4(FORM4_XML)
    assert p["symbol"] == "NVDA" and p["insider_name"] == "DOE JANE" and p["title"] == "Chief Executive Officer"
    types = sorted(t["transaction_type"] for t in p["transactions"])
    assert types == ["BUY", "SELL"]                          # the grant (code A) is excluded
    buy = next(t for t in p["transactions"] if t["transaction_type"] == "BUY")
    assert buy["shares"] == 10000 and buy["price"] == 200.5 and buy["transaction_date"] == "2026-06-01"


def test_parse_form4_bad_xml():
    assert parse_form4("nonsense") is None
    assert parse_form4(None) is None


def test_parse_issuer_form4_refs():
    payload = {"filings": {"recent": {"form": ["4", "10-K", "4"],
               "accessionNumber": ["a1", "x", "a2"], "primaryDocument": ["xsl/wk-form4_1.xml", "y", "wk-form4_2.xml"],
               "filingDate": ["2026-06-01", "2026-05-01", "2026-04-01"]}}}
    refs = parse_issuer_form4_refs(payload, 10)
    assert [r["accession"] for r in refs] == ["a1", "a2"]
    assert refs[0]["primary_doc"] == "xsl/wk-form4_1.xml"


def test_insider_sentiment():
    bull = insider_sentiment([{"transaction_type": "BUY", "shares": 10000, "insider_name": "A"},
                              {"transaction_type": "BUY", "shares": 5000, "insider_name": "B"}])
    assert bull["sentiment"] == "BULLISH" and bull["buy_count"] == 2 and bull["distinct_buyers"] == 2
    bear = insider_sentiment([{"transaction_type": "SELL", "shares": 9000, "insider_name": "C"}])
    assert bear["sentiment"] == "BEARISH" and bear["score"] == 0.0
    assert insider_sentiment([])["sentiment"] is None        # NO DATA, never fabricated


# ------------------------------------------------------------------ collector + read-model (fake providers)
class FakeHoldings:
    _ciks = ["0000000001"]
    def get_holdings_history(self, cik):
        return {"name": "FUND A", "current": {"period": "2026-06-30", "holdings": CUR},
                "previous": {"period": "2026-03-31", "holdings": PREV}}


class FakeInsiders:
    _map = {"NVDA": "x"}
    def get_insider_transactions(self, sym):
        if sym.upper() != "NVDA":
            return []
        return [{"accession": "a1", "insider_name": "DOE JANE", "title": "CEO", "transaction_type": "BUY",
                 "shares": 10000, "price": 200.5, "transaction_date": "2026-06-01", "symbol": "NVDA"},
                {"accession": "a2", "insider_name": "ROE JOHN", "title": "Director", "transaction_type": "BUY",
                 "shares": 5000, "price": 205.0, "transaction_date": "2026-06-02", "symbol": "NVDA"}]


def test_collector_and_flow(store):
    res = InstitutionalCollector(store, FakeHoldings(), FakeInsiders()).collect()
    assert res["changes"] == 3 and res["insiders"] == 2      # (also derives insider clusters, § R1.4)
    flow = build_institutional_flow(store, "NVDA")
    assert flow["status"] == "COMPLETE"
    assert flow["institutional_direction"] == "ACCUMULATION"   # NVDA +100% → bullish
    assert any(c["direction"] == "ACCUMULATION" and c["percentage_change"] == 100.0
               for c in flow["institutional_changes"])
    assert flow["insider_sentiment"] == "BULLISH"
    assert flow["insider_score"] is not None and flow["insider_summary"]["buy_count"] == 2


def test_immutable_history(store):
    c = InstitutionalCollector(store, FakeHoldings(), FakeInsiders())
    c.collect()
    n_changes = store.count_institutional_changes()
    n_insiders = store.count_insider_transactions()
    c.collect()                                              # re-run → same ids, no duplicates/rewrites
    assert store.count_institutional_changes() == n_changes
    assert store.count_insider_transactions() == n_insiders


def test_missing_data_is_no_data(store):
    flow = build_institutional_flow(store, "NVDA")
    assert flow["status"] == "NO DATA"
    assert flow["accumulation_score"] is None and flow["insider_sentiment"] is None
    assert flow["institutional_changes"] == [] and flow["insider_activity"] == []


# ------------------------------------------------------------------ security
def test_no_execution_side_effects(store):
    InstitutionalCollector(store, FakeHoldings(), FakeInsiders()).collect()
    build_institutional_flow(store, "NVDA")
    assert store.list_positions() == []
    assert store.list_fills() == []


def test_source_has_no_broker_or_copytrade_tokens():
    root = Path(__file__).resolve().parents[2] / "src" / "atp"
    files = list((root / "institutional").glob("*.py")) + [root / "services" / "institutional_intelligence.py"]
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi", "copy_trade")
    for f in files:
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
