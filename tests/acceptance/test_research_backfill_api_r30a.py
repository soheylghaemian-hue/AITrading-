"""§ R3.0A.1 control-API acceptance — the dataset endpoints ENQUEUE only; execution is an external worker.

POST /research/datasets performs ZERO provider network I/O and returns promptly with 202 + a PLANNED
dataset; it never holds ctx.lock across provider work. The durable one-shot worker (run OUTSIDE atp-control
with its own DB connection) claims and executes PLANNED datasets. Tests prove: zero provider calls from the
endpoint, prompt 202, the control API stays responsive while the worker is fetching, worker gating, and the
read-model list/detail/coverage after the worker completes. No trading, no order, no execution; live
`ohlc_bars` is never touched.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fastapi import Response

from atp.research import calendars as cal
from atp.research.backfill import (
    MinuteBar, MockAggregatesProvider, build_request, claim_next_one, enqueue_backfill,
)
from atp.store import open_store

NOW = datetime(2023, 12, 1, tzinfo=timezone.utc)


def _control(store):
    import atp.services.control as control
    control.ctx.store = store
    control.ctx.backfill_provider = None
    os.environ["ATP_CONTROL_TOKEN"] = "tok"
    return control


def _store(path=None):
    return open_store(path or (str(Path(tempfile.mkdtemp()) / "atp.db")))


def _minutes(d, base):
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    out, t, i = [], o, 0
    while t < c:
        px = base + Decimal(i) * Decimal("0.01")
        out.append(MinuteBar(ts=t, open=px, high=px + Decimal("0.1"), low=px - Decimal("0.1"),
                             close=px + Decimal("0.02"), volume=Decimal("1000.5"), trade_count=5))
        t += timedelta(minutes=1)
        i += 1
    return out


class _SpyProvider(MockAggregatesProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fetch_calls = 0

    def fetch_minutes(self, *a, **k):
        self.fetch_calls += 1
        return super().fetch_minutes(*a, **k)


def test_post_enqueues_with_zero_provider_calls_and_returns_202():
    store = _store()
    c = _control(store)
    spy = _SpyProvider({}, adjusted=True)
    c.ctx.backfill_provider = spy                       # even if injected, the endpoint must NOT use it

    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-31")
    resp = Response()
    t0 = time.time()
    detail = c.create_dataset(body, resp, authorization="Bearer tok")
    elapsed = time.time() - t0

    assert resp.status_code == 202                      # accepted, not executed
    assert detail["status"] == "PLANNED" and detail["dataset_id"]
    assert detail["request_checksum"]                   # dataset_id + checksum + status returned
    assert spy.fetch_calls == 0                         # ZERO provider network I/O in the request
    assert store.rd_count_bars(detail["dataset_id"]) == 0
    assert elapsed < 0.5                                # returns promptly


def test_post_requires_auth_and_validates_bounds():
    c = _control(_store())
    ok = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    with pytest.raises(c.HTTPException) as e401:
        c.create_dataset(ok, Response(), authorization="Bearer WRONG")
    assert e401.value.status_code == 401
    bad = c.DatasetCreate(symbols=["TSLA"], interval="1D", start="2023-01-03", end="2023-01-04")  # not approved
    with pytest.raises(c.HTTPException) as e422:
        c.create_dataset(bad, Response(), authorization="Bearer tok")
    assert e422.value.status_code == 422


def test_post_is_idempotent_reuse_returns_200():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    c = _control(store)
    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    first = c.create_dataset(body, Response(), authorization="Bearer tok")
    # complete it via the external worker
    claim_next_one(store, MockAggregatesProvider({"NVDA": [m for d in days for m in _minutes(d, Decimal("450"))]},
                                                adjusted=True), now=NOW)
    resp2 = Response()
    again = c.create_dataset(body, resp2, authorization="Bearer tok")
    assert again["dataset_id"] == first["dataset_id"]
    assert again["status"] == "COMPLETED" and resp2.status_code == 200   # reused → 200, not 202


def test_worker_completes_and_read_models_expose_it():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    c = _control(store)
    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    enq = c.create_dataset(body, Response(), authorization="Bearer tok")
    ds_id = enq["dataset_id"]

    listing = c.list_datasets()
    assert listing["datasets"][0]["status"] == "PLANNED"

    # external worker executes it (separate call, its own provider) → COMPLETED
    claim_next_one(store, MockAggregatesProvider({"NVDA": [m for d in days for m in _minutes(d, Decimal("450"))]},
                                                adjusted=True, page_size=300), now=NOW)

    got = c.get_dataset(ds_id)
    assert got["status"] == "COMPLETED" and got["provider_adjusted_flag"] is True
    assert got["adjustment_policy"] == "MASSIVE_SPLIT_ADJUSTED_RTH_V1"
    assert [ev["event_type"] for ev in got["events"]][-1] == "COMPLETE"
    cov = c.get_dataset_coverage(ds_id)
    assert cov["per_symbol"][0]["symbol"] == "NVDA" and cov["per_symbol"][0]["bar_count"] == 2
    with pytest.raises(c.HTTPException) as e404:
        c.get_dataset("nope")
    assert e404.value.status_code == 404


def test_control_api_stays_responsive_while_worker_fetches():
    """The worker runs OUTSIDE atp-control with its OWN store connection; it never holds ctx.lock across
    provider I/O, so a control read completes promptly even while the worker is blocked in a slow fetch."""
    dbfile = str(Path(tempfile.mkdtemp()) / "atp.db")
    store_control = _store(dbfile)
    store_worker = open_store(dbfile, migrate=False)     # the external worker's own connection
    c = _control(store_control)

    days = [date(2023, 1, 3), date(2023, 1, 4)]
    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    c.create_dataset(body, Response(), authorization="Bearer tok")   # enqueue PLANNED

    class _Slow(MockAggregatesProvider):
        def fetch_minutes(self, *a, **k):
            time.sleep(0.8)                                # simulate slow provider network I/O
            return super().fetch_minutes(*a, **k)

    slow = _Slow({"NVDA": [m for d in days for m in _minutes(d, Decimal("450"))]}, adjusted=True)
    worker = threading.Thread(target=lambda: claim_next_one(store_worker, slow, now=NOW))
    worker.start()
    time.sleep(0.15)                                     # let the worker enter the slow fetch

    t0 = time.time()
    listing = c.list_datasets()                          # control read during the worker's fetch
    elapsed = time.time() - t0
    assert elapsed < 0.4                                 # NOT blocked by the worker's provider I/O
    assert listing["count"] == 1
    worker.join(timeout=10)
    assert store_control.rd_get_dataset(listing["datasets"][0]["dataset_id"]).status == "COMPLETED"


def test_worker_provider_gating_never_exposes_key(monkeypatch):
    from atp.research.backfill import worker as w
    monkeypatch.delenv("ATP_BACKFILL_ENABLED", raising=False)
    prov, reason = w.build_provider_from_env()
    assert prov is None and "disabled" in reason.lower()
    monkeypatch.setenv("ATP_BACKFILL_ENABLED", "1")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    prov2, reason2 = w.build_provider_from_env()
    assert prov2 is None and "MASSIVE_API_KEY" in reason2
    monkeypatch.setenv("MASSIVE_API_KEY", "super-secret-key")
    prov3, reason3 = w.build_provider_from_env()
    assert prov3 is not None and reason3 is None         # configured; the reason string never carries the key
    assert reason2 is not None and "super-secret-key" not in reason2
