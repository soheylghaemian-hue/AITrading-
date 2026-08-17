"""§ R3.0A control-API acceptance — research dataset endpoints (auth, disabled-by-default, read-models).

The real paid backfill is DOUBLE-gated (ATP_BACKFILL_ENABLED + MASSIVE_API_KEY); without that, POST
/research/datasets returns 403 BACKFILL_DISABLED so no live/paid request is ever issued. Tests inject a
MockAggregatesProvider to exercise the create + list + detail + coverage read-models. No trading, no order,
no execution; live `ohlc_bars` is never touched.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.backfill import MinuteBar, MockAggregatesProvider
from atp.store import open_store

NOW = datetime(2023, 12, 1, tzinfo=timezone.utc)


def _control(store, provider=None):
    import atp.services.control as control
    control.ctx.store = store
    control.ctx.backfill_provider = provider
    os.environ["ATP_CONTROL_TOKEN"] = "tok"
    return control


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


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


def test_backfill_disabled_by_default_returns_403():
    from fastapi import HTTPException
    c = _control(_store(), provider=None)                 # no injected provider, env not enabled
    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    with pytest.raises(HTTPException) as e:
        c.create_dataset(body, authorization="Bearer tok")
    assert e.value.status_code == 403 and e.value.detail["detail"] == "BACKFILL_DISABLED"


def test_create_list_detail_coverage_with_injected_provider():
    from fastapi import HTTPException
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in _minutes(d, Decimal("450"))]},
                                  adjusted=True, page_size=300)
    store = _store()
    c = _control(store, provider=prov)

    body = c.DatasetCreate(symbols=["NVDA"], interval="1D", start="2023-01-03", end="2023-01-04")
    with pytest.raises(HTTPException) as e401:
        c.create_dataset(body, authorization="Bearer WRONG")
    assert e401.value.status_code == 401

    # NOTE: the mock provider generates minutes with today's real clock inside run_backfill's default now;
    # so pin a completed 'now' by monkeypatching is unnecessary — run_backfill uses datetime.now default,
    # and 2023 sessions are always completed relative to the real clock.
    detail = c.create_dataset(body, authorization="Bearer tok")
    assert detail["status"] == "COMPLETED" and detail["row_count"] == 2
    ds_id = detail["dataset_id"]

    listing = c.list_datasets()
    assert listing["count"] == 1 and listing["datasets"][0]["dataset_id"] == ds_id

    got = c.get_dataset(ds_id)
    assert got["dataset_id"] == ds_id and got["provider_adjusted_flag"] is True
    assert got["adjustment_policy"] == "MASSIVE_SPLIT_ADJUSTED_RTH_V1"
    assert [ev["event_type"] for ev in got["events"]][-1] == "COMPLETE"

    cov = c.get_dataset_coverage(ds_id)
    assert cov["per_symbol"][0]["symbol"] == "NVDA" and cov["per_symbol"][0]["bar_count"] == 2

    with pytest.raises(HTTPException) as e404:
        c.get_dataset("nope")
    assert e404.value.status_code == 404


def test_create_dataset_rejects_out_of_bounds_request():
    from fastapi import HTTPException
    c = _control(_store(), provider=MockAggregatesProvider({}, adjusted=True))
    bad = c.DatasetCreate(symbols=["TSLA"], interval="1D", start="2023-01-03", end="2023-01-04")  # not approved
    with pytest.raises(HTTPException) as e:
        c.create_dataset(bad, authorization="Bearer tok")
    assert e.value.status_code == 422
