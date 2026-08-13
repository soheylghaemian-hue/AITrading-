"""Dashboard API layer — RiskConfig GET/UPDATE over HTTP, auth, no-secret-leak, no fake data.

Uses FastAPI's TestClient against the real create_app. Skips cleanly (functions return) if
FastAPI isn't installed, so the offline runner stays green either way.
"""

import os
import tempfile
from pathlib import Path

from atp.brokers.base import Account
from atp.dashboard.api import DashboardContext, create_app
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore

try:
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

TOKEN = "unit-test-token"


class _FakeBroker:
    def __init__(self, equity=1_000_000.0):
        self._e = equity
    async def get_account(self):
        return Account(cash=self._e, equity=self._e, realized_pnl=0.0, unrealized_pnl=0.0,
                       gross_exposure=0.0, net_exposure=0.0, positions={})
    async def get_positions(self):
        return {}
    def is_connected(self):
        return True


def _client(*, read_token=None, rate_limit=None, cors=None):
    for k in ("ATP_DASHBOARD_READ_TOKEN", "ATP_DASHBOARD_RATE_LIMIT", "ATP_DASHBOARD_CORS_ORIGINS"):
        os.environ.pop(k, None)
    os.environ["ATP_DASHBOARD_TOKEN"] = TOKEN
    if read_token:
        os.environ["ATP_DASHBOARD_READ_TOKEN"] = read_token
    if rate_limit:
        os.environ["ATP_DASHBOARD_RATE_LIMIT"] = str(rate_limit)
    if cors:
        os.environ["ATP_DASHBOARD_CORS_ORIGINS"] = cors
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0))
    store = RiskConfigStore(str(Path(tempfile.mkdtemp()) / "risk.json"))
    ctx = DashboardContext(broker=_FakeBroker(), risk=risk, mode="paper",
                           execution_enabled=False, config_store=store)
    return TestClient(create_app(ctx)), ctx


def test_summary_exposes_trading_risk_and_leaks_no_secret():
    if not _HAS_FASTAPI:
        return
    client, _ = _client()
    r = client.get("/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_risk"] is not None
    assert body["mode"] == "paper" and body["execution_enabled"] is False
    assert TOKEN not in r.text                       # the owner token is never in a response


def test_riskconfig_unauthorized_is_rejected():
    if not _HAS_FASTAPI:
        return
    client, _ = _client()
    payload = {"capital": 500000, "risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.02}
    assert client.post("/dashboard/risk-config", json=payload).status_code == 401           # no token
    assert client.post("/dashboard/risk-config", json=payload,
                       headers={"Authorization": "Bearer WRONG"}).status_code == 401         # bad token


def test_riskconfig_update_applies_and_reads_back():
    if not _HAS_FASTAPI:
        return
    client, ctx = _client()
    payload = {"capital": 500000, "risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.02}
    r = client.post("/dashboard/risk-config", json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["max_risk_per_trade"] == 5000.0 and r.json()["max_daily_loss"] == 10000.0
    # authoritative Risk Engine really updated
    assert ctx.risk.limits.max_trade_risk_pct == 0.01 and ctx.risk.limits.max_daily_loss_pct == 0.02
    # read back through the API
    tr = client.get("/dashboard/trading-risk").json()
    assert tr["capital"] == 500000.0 and tr["max_daily_loss"] == 10000.0
    # persisted to disk
    assert ctx.config_store.load().capital == 500000.0


def test_riskconfig_invalid_is_400():
    if not _HAS_FASTAPI:
        return
    client, _ = _client()
    bad = {"capital": 500000, "risk_per_trade_pct": 0.05, "max_daily_loss_pct": 0.02}  # risk > daily
    r = client.post("/dashboard/risk-config", json=bad, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- Phase 6: secure public API
def test_read_endpoints_require_read_token_when_configured():
    if not _HAS_FASTAPI:
        return
    client, _ = _client(read_token="read-secret")
    assert client.get("/dashboard/summary").status_code == 401                     # no token → blocked
    assert client.get("/dashboard/summary",
                      headers={"Authorization": "Bearer WRONG"}).status_code == 401  # bad token
    ok = client.get("/dashboard/summary", headers={"Authorization": "Bearer read-secret"})
    assert ok.status_code == 200 and ok.json()["mode"] == "paper"


def test_reads_open_when_no_read_token_set():
    if not _HAS_FASTAPI:
        return
    client, _ = _client()   # no read token → reads open (local dev)
    assert client.get("/dashboard/summary").status_code == 200


def test_cors_locked_to_production_origin():
    if not _HAS_FASTAPI:
        return
    client, _ = _client(cors="https://www.gigbay.de")
    good = client.get("/dashboard/summary", headers={"Origin": "https://www.gigbay.de"})
    assert good.headers.get("access-control-allow-origin") == "https://www.gigbay.de"
    bad = client.get("/dashboard/summary", headers={"Origin": "https://evil.example.com"})
    assert bad.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_rate_limit_returns_429():
    if not _HAS_FASTAPI:
        return
    client, _ = _client(rate_limit=3)
    codes = [client.get("/api/health").status_code for _ in range(5)]
    assert 429 in codes and codes.count(200) == 3   # first 3 ok, then throttled
