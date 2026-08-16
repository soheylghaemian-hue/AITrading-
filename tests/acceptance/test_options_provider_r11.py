"""Phase R1.1 — Options Data Provider Activation (audit, DATA ONLY).

Covers the entitlement probe + audit: provider connection (200 → entitled + parsed contracts),
authentication failure (401), missing entitlement (403 NOT_AUTHORIZED), real-response parsing, the
audit verdict + recommended providers, no fabricated values, and no key exposure. Touches no Trading
Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from atp.optflow.diagnostics import RECOMMENDED_PROVIDERS, audit_options_provider
from atp.optflow.provider import NullOptionsProvider, PolygonOptionsProvider, parse_polygon_options

POLY_SNAPSHOT = {"status": "OK", "results": [
    {"details": {"contract_type": "call", "strike_price": 210, "expiration_date": "2026-09-18"},
     "day": {"volume": 20000, "close": 5.1}, "open_interest": 15000, "implied_volatility": 0.42,
     "last_quote": {"bid": 5.0, "ask": 5.2}, "last_trade": {"price": 5.1}},
    {"details": {"contract_type": "put", "strike_price": 200, "expiration_date": "2026-09-18"},
     "day": {"volume": 8000, "close": 4.1}, "open_interest": 10000, "implied_volatility": 0.40,
     "last_quote": {"bid": 4.0, "ask": 4.2}, "last_trade": {"price": 4.1}},
]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode()
        self.status = status
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http_error(code, body):
    return HTTPError("https://api.polygon.io/x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


# ------------------------------------------------------------------ probe outcomes
def test_probe_no_key_is_no_data():
    r = PolygonOptionsProvider(api_key="").probe("NVDA")
    assert r["configured"] is False
    assert r["entitled"] is False
    assert r["reason"] == "no_api_key"
    assert r["contracts"] == 0                              # nothing fabricated


def test_provider_connection_entitled_parses(monkeypatch):
    monkeypatch.setattr("atp.optflow.provider.urlopen", lambda req, timeout=None: FakeResp(POLY_SNAPSHOT))
    r = PolygonOptionsProvider(api_key="KEY").probe("NVDA")
    assert r["entitled"] is True
    assert r["http_status"] == 200
    assert r["upstream_status"] == "OK"
    assert r["contracts"] == 2                              # both call + put parsed


def test_missing_entitlement_403_not_authorized(monkeypatch):
    def raise_403(req, timeout=None):
        raise _http_error(403, {"status": "NOT_AUTHORIZED", "message": "not entitled for options"})
    monkeypatch.setattr("atp.optflow.provider.urlopen", raise_403)
    r = PolygonOptionsProvider(api_key="KEY").probe("NVDA")
    assert r["entitled"] is False
    assert r["http_status"] == 403
    assert r["reason"] == "not_entitled"
    assert r["upstream_status"] == "NOT_AUTHORIZED"         # Polygon's own status word (not the key)
    assert r["contracts"] == 0


def test_authentication_failure_401(monkeypatch):
    def raise_401(req, timeout=None):
        raise _http_error(401, {"status": "ERROR", "message": "unknown api key"})
    monkeypatch.setattr("atp.optflow.provider.urlopen", raise_401)
    r = PolygonOptionsProvider(api_key="BADKEY").probe("NVDA")
    assert r["entitled"] is False
    assert r["http_status"] == 401
    assert r["reason"] == "auth_failed"


def test_unreachable_is_no_data_not_fabricated(monkeypatch):
    def boom(req, timeout=None):
        raise URLError("connection refused")
    monkeypatch.setattr("atp.optflow.provider.urlopen", boom)
    r = PolygonOptionsProvider(api_key="KEY").probe("NVDA")
    assert r["reason"] == "unreachable"
    assert r["contracts"] == 0


def test_real_response_parsing():
    cs = parse_polygon_options(POLY_SNAPSHOT, "NVDA")
    assert len(cs) == 2
    call = next(c for c in cs if c.option_type == "call")
    assert call.strike == 210 and call.volume == 20000 and call.open_interest == 15000
    assert call.implied_volatility == 0.42
    assert parse_polygon_options({}, "NVDA") == []          # empty → NO DATA, never fabricated


# ------------------------------------------------------------------ audit verdict
def test_audit_not_available_recommends_providers(monkeypatch):
    # No key → probe reports no data → NOT AVAILABLE with recommended licensed providers (Task 3).
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    monkeypatch.setenv("ATP_OPTIONS_PROVIDER", "polygon")
    a = audit_options_provider(["NVDA", "AAPL", "SPY"])
    assert a["options_access"] == "NOT AVAILABLE"
    assert a["recommended_providers"] == RECOMMENDED_PROVIDERS
    names = " ".join(p["name"] for p in a["recommended_providers"])
    assert "Polygon" in names and "CBOE" in names and "ORATS" in names and "Tradier" in names
    assert set(a["symbols"]) == {"NVDA", "AAPL", "SPY"}


def test_audit_available_when_entitled(monkeypatch):
    monkeypatch.setattr("atp.optflow.provider.urlopen", lambda req, timeout=None: FakeResp(POLY_SNAPSHOT))
    monkeypatch.setenv("MASSIVE_API_KEY", "KEY")
    monkeypatch.setenv("ATP_OPTIONS_PROVIDER", "polygon")
    a = audit_options_provider(["NVDA"])
    assert a["options_access"] == "AVAILABLE"
    assert a["recommended_providers"] is None
    assert a["symbols"]["NVDA"]["contracts"] == 2


def test_null_provider_audit_is_not_available(monkeypatch):
    monkeypatch.setenv("ATP_OPTIONS_PROVIDER", "null")
    a = audit_options_provider(["NVDA"])
    assert a["provider"] == "null"
    assert a["options_access"] == "NOT AVAILABLE"
    assert NullOptionsProvider().probe("NVDA")["entitled"] is False


# ------------------------------------------------------------------ security: no key exposure, no execution
def test_probe_never_exposes_key_in_url(monkeypatch):
    seen = {}
    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return FakeResp(POLY_SNAPSHOT)
    monkeypatch.setattr("atp.optflow.provider.urlopen", capture)
    r = PolygonOptionsProvider(api_key="SECRETKEY123").probe("NVDA")
    assert "SECRETKEY123" not in seen["url"]                # key never in the URL
    assert seen["auth"] == "Bearer SECRETKEY123"            # only in the header
    assert "SECRETKEY123" not in json.dumps(r)             # never leaked into the probe result


def test_diagnostics_source_has_no_broker_tokens():
    root = Path(__file__).resolve().parents[2] / "src" / "atp" / "optflow"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi")
    for f in (root / "provider.py", root / "diagnostics.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
