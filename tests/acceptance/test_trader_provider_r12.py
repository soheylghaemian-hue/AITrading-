"""Phase R1.2 — Trader Intelligence live provider (SEC 13F) activation (DATA ONLY).

Covers: provider registration + configuration, SEC submissions + 13F info-table parsing (default and
prefixed namespaces, put→bearish), holding→position mapping, a mocked provider connection, real data
flowing through the Quality + Consensus engines, missing-provider NO DATA, data-completeness change, the
provider audit, no fabrication, and no execution / broker access. Touches no Trading Core / Risk /
Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atp.completeness.engine import compute_completeness
from atp.traders.collector import TraderCollector
from atp.traders.diagnostics import audit_trader_providers
from atp.traders.provider import NullTraderProvider, resolve_provider
from atp.traders.readmodel import build_symbol_consensus
from atp.traders.sec13f import (
    DEFAULT_CUSIPS, Sec13FTraderProvider, holding_to_position, parse_info_table, parse_submissions,
)
from atp.store import open_store

SUBMISSIONS = {"name": "TEST CAPITAL LLC", "cik": 123, "filings": {"recent": {
    "form": ["13F-HR", "10-K", "13F-HR"],
    "accessionNumber": ["0000000000-26-000002", "0000000000-26-000009", "0000000000-24-000001"],
    "filingDate": ["2026-05-15", "2026-03-01", "2024-05-15"],
    "primaryDocument": ["primary_doc.xml", "x.htm", "primary_doc.xml"]}}}
INDEX = {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "infotable.xml"}]}}
# Default-namespace info table: NVDA long + AAPL via PUT (bearish).
INFOTABLE = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>NVIDIA CORP</nameOfIssuer><cusip>67066G104</cusip><value>1150000</value>
    <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>600000</value>
    <shrsOrPrnAmt><sshPrnamt>2000</sshPrnamt></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
  <infoTable><nameOfIssuer>FOREIGN CO</nameOfIssuer><cusip>999999999</cusip><value>10</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>"""
# Prefixed-namespace variant (some filers use ns1:) — must parse the same.
INFOTABLE_NS = """<?xml version="1.0"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable><ns1:nameOfIssuer>NVIDIA</ns1:nameOfIssuer><ns1:cusip>67066G104</ns1:cusip>
    <ns1:value>2000</ns1:value><ns1:shrsOrPrnAmt><ns1:sshPrnamt>10</ns1:sshPrnamt></ns1:shrsOrPrnAmt></ns1:infoTable>
</ns1:informationTable>"""


class FakeResp:
    def __init__(self, body: bytes): self._b = body; self.status = 200
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_urlopen(req, timeout=None):
    url = req.full_url
    if "/submissions/" in url:
        return FakeResp(json.dumps(SUBMISSIONS).encode())
    if url.endswith("index.json"):
        return FakeResp(json.dumps(INDEX).encode())
    if url.endswith(".xml"):
        return FakeResp(INFOTABLE.encode())
    return FakeResp(b"{}")


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))


# ------------------------------------------------------------------ registration + config
def test_provider_registered(monkeypatch):
    monkeypatch.setenv("ATP_TRADER_PROVIDER", "sec13f")
    monkeypatch.setenv("ATP_SEC_USER_AGENT", "GIGBAY test test@example.com")
    p = resolve_provider()
    assert isinstance(p, Sec13FTraderProvider)
    assert p.configured is True


def test_not_configured_without_user_agent():
    p = Sec13FTraderProvider(ciks=["0000000123"], user_agent="")
    assert p.configured is False
    assert p.get_traders() == []                            # no UA → no fetch → NO DATA (never fabricated)
    assert p.get_positions("0000000123") == []


# ------------------------------------------------------------------ parsers
def test_parse_submissions():
    s = parse_submissions(SUBMISSIONS)
    assert s["name"] == "TEST CAPITAL LLC"
    assert s["cik"] == "0000000123"
    assert s["latest"]["date"] == "2026-05-15"              # newest 13F-HR
    assert s["first_13f_date"] == "2024-05-15"              # oldest 13F-HR
    assert s["filing_count"] == 2
    assert parse_submissions({"name": "X", "filings": {"recent": {"form": ["10-K"]}}})["latest"] is None


def test_parse_info_table_default_namespace():
    h = {x["symbol"]: x for x in parse_info_table(INFOTABLE, DEFAULT_CUSIPS)}
    assert set(h) == {"NVDA", "AAPL"}                       # foreign 999999999 dropped
    assert h["NVDA"]["long_shares"] == 5000 and h["NVDA"]["put_shares"] == 0
    assert h["AAPL"]["put_shares"] == 2000 and h["AAPL"]["long_shares"] == 0   # PUT → bearish bucket


def test_parse_info_table_prefixed_namespace():
    h = parse_info_table(INFOTABLE_NS, DEFAULT_CUSIPS)      # ns1: prefixed must parse the same
    assert len(h) == 1 and h[0]["symbol"] == "NVDA" and h[0]["long_shares"] == 10


def test_parse_info_table_bad_xml_is_empty():
    assert parse_info_table("not xml", DEFAULT_CUSIPS) == []
    assert parse_info_table(None, DEFAULT_CUSIPS) == []     # NO DATA, never fabricated


def test_holding_to_position_directions():
    long_p = holding_to_position("0000000123", {"symbol": "NVDA", "cusip": "x", "long_shares": 5000, "put_shares": 0, "value": 1150000}, "T")
    assert long_p.direction == "LONG" and long_p.position_size == 5000.0 and long_p.entry_price == 230.0
    short_p = holding_to_position("0000000123", {"symbol": "AAPL", "cusip": "x", "long_shares": 0, "put_shares": 2000, "value": 600000}, "T")
    assert short_p.direction == "SHORT" and short_p.position_size == 2000.0
    assert holding_to_position("0000000123", {"symbol": "X", "cusip": "x", "long_shares": 0, "put_shares": 0, "value": 0}, "T") is None


# ------------------------------------------------------------------ mocked provider connection
def test_provider_connection_parses_real_shape(monkeypatch):
    monkeypatch.setattr("atp.traders.sec13f.urlopen", fake_urlopen)
    p = Sec13FTraderProvider(ciks=["0000000123"], user_agent="GIGBAY test test@example.com")
    traders = p.get_traders()
    assert len(traders) == 1 and traders[0].name == "TEST CAPITAL LLC" and traders[0].source == "SEC 13F"
    assert traders[0].track_record_days and traders[0].track_record_days > 300   # from 2024-05-15
    pos = {x.symbol: x for x in p.get_positions("0000000123")}
    assert pos["NVDA"].direction == "LONG"
    assert pos["AAPL"].direction == "SHORT"                 # the put
    assert p.get_performance("0000000123") is not None      # empty record → track-record quality flows


# ------------------------------------------------------------------ quality + consensus flow
def test_quality_and_consensus_flow(monkeypatch, store):
    monkeypatch.setattr("atp.traders.sec13f.urlopen", fake_urlopen)
    p = Sec13FTraderProvider(ciks=["0000000123"], user_agent="GIGBAY test test@example.com")
    assert TraderCollector(store, p).collect() == 1
    nvda = build_symbol_consensus(store, "NVDA")
    assert nvda["consensus"] == "BULLISH" and nvda["long_percent"] == 100.0
    assert nvda["weighted_score"] is not None and nvda["weighted_score"] > 0   # track-record quality
    aapl = build_symbol_consensus(store, "AAPL")
    assert aapl["consensus"] == "BEARISH"                   # institution holds AAPL puts


def test_missing_provider_is_no_data(store):
    assert TraderCollector(store, NullTraderProvider()).collect() == 0
    c = build_symbol_consensus(store, "NVDA")
    assert c["consensus"] is None and c["contributor_count"] == 0   # NO DATA, never fabricated


def test_completeness_reflects_traders(monkeypatch, store):
    monkeypatch.setattr("atp.traders.sec13f.urlopen", fake_urlopen)
    assert "trader" in compute_completeness(store, "NVDA")["missing"]
    TraderCollector(store, Sec13FTraderProvider(ciks=["0000000123"], user_agent="ua x@y.z")).collect()
    assert "trader" in compute_completeness(store, "NVDA")["available"]


# ------------------------------------------------------------------ audit + security
def test_audit_selects_sec13f():
    a = audit_trader_providers()
    assert a["selected_provider"] == "SEC 13F (EDGAR)"
    names = " ".join(p["provider"] for p in a["providers"])
    for src in ("SEC 13F", "Darwinex", "Collective2", "eToro", "TradingView"):
        assert src in names


def test_audit_not_available_without_provider(monkeypatch):
    monkeypatch.delenv("ATP_TRADER_PROVIDER", raising=False)
    a = audit_trader_providers()
    assert a["active_provider"] == "null"
    assert a["trader_access"] == "NOT AVAILABLE"


def test_no_execution_or_broker_tokens():
    root = Path(__file__).resolve().parents[2] / "src" / "atp" / "traders"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi", "copy_trade")
    for f in (root / "sec13f.py", root / "diagnostics.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
