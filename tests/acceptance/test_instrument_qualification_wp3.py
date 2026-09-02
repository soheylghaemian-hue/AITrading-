"""§ WP3 acceptance — controlled, read-only IBKR verification & tradability check (REFERENCE DATA ONLY).

End-to-end over the durable store + async orchestrator with a scripted fake IBKR client:
  * migration 27 applies; instruments default to DISCOVERED;
  * every status is reachable (VERIFIED / AMBIGUOUS / NOT_TRADABLE / MARKET_DATA_NOT_ENTITLED /
    ERROR_RETRYABLE / ERROR_PERMANENT) and only a UNIQUE contract is VERIFIED (con_id + last_verified_at set);
  * rate-limit pause between batches; configurable batch size;
  * idempotent re-run (terminal skipped, ERROR_RETRYABLE retried) and retry→permanent escalation;
  * resume of a crashed RUNNING run (PENDING re-selected, VERIFIED untouched);
  * per-instrument AND per-market error isolation;
  * observable progress (run counters + immutable audit events); conId-collision guard ⇒ AMBIGUOUS;
  * missing IBKR connection ⇒ visible ERROR status and the run FAILED;
  * DB-enforced immutability of events + terminal runs; stale-run reclaim.

SAFETY: only reqContractDetails-style reads; no orders/execution/market-data purchase/trading activation.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.instruments.qualification import (
    ConnectionUnavailableError,
    MarketDataNotEntitledError,
    PermanentQualificationError,
    QualificationConfig,
    RetryableQualificationError,
    qualification_request_checksum,
    qualify_instruments,
)
from atp.store import open_store


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
    """Fake read-only IBKR client. `script[symbol]` is a tag or ("verified", conid). Records every call."""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[str] = []

    async def fetch_contract_details(self, request):
        self.calls.append(request.symbol)
        b = self.script.get(request.symbol, "empty")
        if isinstance(b, tuple) and b[0] == "verified":
            return [detail(b[1], request.symbol)]
        if isinstance(b, tuple) and b[0] == "retry":       # ("retry", custom message)
            raise RetryableQualificationError(b[1], code="100")
        if isinstance(b, tuple) and b[0] == "connection":  # ("connection", custom message)
            raise ConnectionUnavailableError(b[1])
        if b == "ambiguous":
            return [detail(9001, request.symbol), detail(9002, request.symbol)]
        if b == "mdne":
            raise MarketDataNotEntitledError("market data not subscribed", code="10089")
        if b == "retry":
            raise RetryableQualificationError("pacing violation", code="100")
        if b == "permanent":
            raise PermanentQualificationError("invalid request", code="321")
        if b == "connection":
            raise ConnectionUnavailableError("IBKR connection unavailable")
        if b == "unknown":
            raise ValueError("unexpected client fault")
        return []


def _upsert(store, symbol, *, exchange="NASDAQ", asset_class=AssetClass.EQUITY, currency="USD"):
    rec = InstrumentRecord(symbol=symbol, asset_class=asset_class, exchange=exchange, trading_currency=currency,
                           region="AMERICAS", country="US", timezone="America/New_York",
                           trading_calendar="us_equity", multiplier="1", primary_exchange=exchange, source="t")
    store.im_upsert_instrument(rec.as_record())
    return rec.instrument_id


# --------------------------------------------------------------------- migration + defaults
def test_migration_27_applied_and_defaults_discovered():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {26, 27} <= versions and max(versions) >= 27
    iid = _upsert(store, "MSFT")
    row = store.im_get_instrument(iid)
    assert row.qualification_status == "DISCOVERED" and row.qualification_attempts == 0
    assert row.last_qualified_at is None


# --------------------------------------------------------------------- all outcomes in one run
async def test_all_outcomes_reachable_and_counted():
    store = _store()
    ids = {s: _upsert(store, s) for s in ["VER", "AMB", "GONE", "NOMD", "RETRY"]}
    client = ScriptedClient({"VER": ("verified", 1), "AMB": "ambiguous", "GONE": "empty",
                             "NOMD": "mdne", "RETRY": "retry"})
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    summary = await qualify_instruments(store, client, run_label="all",
                                        config=QualificationConfig(batch_size=2, pause_seconds=0.25),
                                        sleep=fake_sleep)
    assert summary.status == "COMPLETED"
    assert (summary.verified, summary.ambiguous, summary.not_tradable,
            summary.market_data_not_entitled, summary.error_retryable) == (1, 1, 1, 1, 1)
    assert summary.processed == 5
    assert sleeps and all(s == 0.25 for s in sleeps)                 # rate-limit pause applied

    ver = store.im_get_instrument(ids["VER"])
    assert ver.qualification_status == "VERIFIED" and ver.con_id == 1
    assert ver.verification_status == "verified" and ver.last_verified_at is not None
    assert store.im_get_instrument(ids["AMB"]).qualification_status == "AMBIGUOUS"
    assert store.im_get_instrument(ids["AMB"]).con_id is None       # ambiguous never assigns a conId
    assert store.im_get_instrument(ids["GONE"]).qualification_status == "NOT_TRADABLE"
    assert store.im_get_instrument(ids["NOMD"]).qualification_status == "MARKET_DATA_NOT_ENTITLED"
    assert store.im_get_instrument(ids["RETRY"]).qualification_status == "ERROR_RETRYABLE"
    # observable audit trail
    events = store.iq_list_run_events(summary.run_id)
    assert any(e.event_type == "QUALIFY_RESULT" and e.status == "VERIFIED" for e in events)
    assert any(e.event_type == "MARKET_OK" for e in events)


# --------------------------------------------------------------------- idempotent re-run + escalation
async def test_rerun_skips_terminal_and_retries_then_escalates():
    store = _store()
    ok = _upsert(store, "VER")
    bad = _upsert(store, "RETRY")
    client = ScriptedClient({"VER": ("verified", 5), "RETRY": "retry"})
    cfg = QualificationConfig(batch_size=10, pause_seconds=0.0, max_attempts=2)

    async def no_sleep(_):
        return None

    s1 = await qualify_instruments(store, client, run_label="r", config=cfg, sleep=no_sleep)
    assert s1.processed == 2 and store.im_get_instrument(bad).qualification_status == "ERROR_RETRYABLE"

    client2 = ScriptedClient({"VER": ("verified", 5), "RETRY": "retry"})
    s2 = await qualify_instruments(store, client2, run_label="r", config=cfg, sleep=no_sleep)
    assert client2.calls == ["RETRY"]                       # VERIFIED skipped; only ERROR_RETRYABLE retried
    assert store.im_get_instrument(bad).qualification_status == "ERROR_PERMANENT"   # attempts==2 => escalate
    assert store.im_get_instrument(bad).qualification_attempts == 2
    assert s2.run_id != s1.run_id                           # a COMPLETED run does not block a fresh pass

    client3 = ScriptedClient({"RETRY": "retry"})
    s3 = await qualify_instruments(store, client3, run_label="r", config=cfg, sleep=no_sleep)
    assert client3.calls == [] and s3.processed == 0        # ERROR_PERMANENT is not re-selected


# --------------------------------------------------------------------- resume of a crashed RUNNING run
async def test_resume_reprocesses_pending_and_leaves_verified_untouched():
    store = _store()
    a = _upsert(store, "AAA")
    b = _upsert(store, "BBB")
    checksum = qualification_request_checksum("resume", None, False)
    store.iq_create_run(run_id="run-x", request_checksum=checksum, run_label="resume", exchange=None,
                        batch_size=25, pause_seconds=1.0)
    store.iq_advance_run_status("run-x", "PLANNED", "RUNNING")
    # A already verified in the interrupted pass; B was claimed (PENDING) when the worker crashed.
    store.iq_apply_outcome(a, run_id="run-x", qualification_status="VERIFIED", reason="prior",
                           verification_status="verified", con_id=1, set_last_verified=True,
                           event={"id": "run-x-e1", "seq": 1, "instrument_id": a, "event_type": "QUALIFY_RESULT",
                                  "status": "VERIFIED"})
    store.iq_mark_pending(b, "run-x")

    # A's provider MUST NOT be called on resume (it is already VERIFIED and not selectable).
    client = ScriptedClient({"BBB": ("verified", 2)})  # AAA absent → would return [] if wrongly called
    summary = await qualify_instruments(store, client, run_label="resume",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    # a FRESH run (never re-enters the live RUNNING row), but it picks up the incomplete work by selection
    assert summary.resumed is True and summary.run_id != "run-x"
    assert client.calls == ["BBB"]                         # A (VERIFIED) is not re-selected
    assert store.im_get_instrument(a).qualification_status == "VERIFIED" and store.im_get_instrument(a).con_id == 1
    assert store.im_get_instrument(b).qualification_status == "VERIFIED"


async def _noop():
    return None


# --------------------------------------------------------------------- per-instrument isolation
async def test_per_instrument_error_isolation():
    store = _store()
    ids = {s: _upsert(store, s) for s in ["OK1", "BADX", "OK2"]}
    client = ScriptedClient({"OK1": ("verified", 1), "BADX": "unknown", "OK2": ("verified", 2)})
    summary = await qualify_instruments(store, client, run_label="iso",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "COMPLETED"                     # a single bad instrument never aborts the market
    assert store.im_get_instrument(ids["OK1"]).qualification_status == "VERIFIED"
    assert store.im_get_instrument(ids["OK2"]).qualification_status == "VERIFIED"
    assert store.im_get_instrument(ids["BADX"]).qualification_status == "ERROR_RETRYABLE"


# --------------------------------------------------------------------- per-market isolation
class _MarketFailStore:
    """Delegating store proxy that raises an infra error when marking a specific market's instruments."""

    def __init__(self, store, fail_ids):
        self._s = store
        self._fail = set(fail_ids)

    def __getattr__(self, name):
        return getattr(self._s, name)

    def iq_mark_pending(self, instrument_id, run_id):
        if instrument_id in self._fail:
            raise RuntimeError("market infrastructure unavailable")
        return self._s.iq_mark_pending(instrument_id, run_id)


async def test_per_market_error_isolation_gives_partial():
    store = _store()
    good = _upsert(store, "GOOD", exchange="NASDAQ")
    bad = _upsert(store, "BADM", exchange="BADMKT")
    proxy = _MarketFailStore(store, {bad})
    client = ScriptedClient({"GOOD": ("verified", 1), "BADM": ("verified", 2)})
    summary = await qualify_instruments(proxy, client, run_label="mkt",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "PARTIAL"
    assert summary.completed_markets == ["NASDAQ"] and summary.failed_markets == ["BADMKT"]
    assert store.im_get_instrument(good).qualification_status == "VERIFIED"
    assert store.im_get_instrument(bad).qualification_status in ("DISCOVERED", "QUALIFICATION_PENDING")


# --------------------------------------------------------------------- conId collision guard
async def test_conid_collision_yields_ambiguous():
    store = _store()
    a = _upsert(store, "TWINA")
    b = _upsert(store, "TWINB")
    client = ScriptedClient({"TWINA": ("verified", 777), "TWINB": ("verified", 777)})  # same contract
    await qualify_instruments(store, client, run_label="dup",
                              config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    statuses = {store.im_get_instrument(a).qualification_status, store.im_get_instrument(b).qualification_status}
    assert statuses == {"VERIFIED", "AMBIGUOUS"}             # exactly one wins the conId; the other is ambiguous
    owners = [i for i in (a, b) if store.im_get_instrument(i).con_id == 777]
    assert len(owners) == 1                                  # conId assigned to exactly one instrument


# --------------------------------------------------------------------- missing IBKR connection
async def test_missing_connection_is_visible_error_and_run_failed():
    store = _store()
    a = _upsert(store, "C1")
    _upsert(store, "C2")

    class DownClient:
        async def fetch_contract_details(self, request):
            raise ConnectionUnavailableError("IBKR connection unavailable")

    summary = await qualify_instruments(store, DownClient(), run_label="down",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "FAILED" and summary.connection_lost is True
    assert store.im_get_instrument(a).qualification_status == "ERROR_RETRYABLE"
    run = store.iq_get_run(summary.run_id)
    assert run.failure_code == "CONNECTION_UNAVAILABLE"


# --------------------------------------------------------------------- immutability + reclaim
async def test_events_and_terminal_runs_are_immutable():
    store = _store()
    _upsert(store, "X")
    summary = await qualify_instruments(store, ScriptedClient({"X": ("verified", 1)}),
                                        run_label="imm", config=QualificationConfig(pause_seconds=0.0),
                                        sleep=lambda _: _noop())
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE instrument_qualification_events SET severity='X' WHERE run_id=?",
                        (summary.run_id,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE instrument_qualification_runs SET run_label='x' WHERE run_id=?",
                        (summary.run_id,))


async def test_connection_loss_detected_by_type_not_message():
    """Regression: a real ConnectionUnavailableError whose message does NOT start with 'IBKR connection'
    must still abort the run (detection is by exception TYPE)."""
    store = _store()
    ids = {s: _upsert(store, s) for s in ["K1", "K2"]}
    msg = "Connectivity between IB and TWS has been lost"   # does NOT start with 'IBKR connection'
    client = ScriptedClient({"K1": ("connection", msg), "K2": ("connection", msg)})
    summary = await qualify_instruments(store, client, run_label="ct",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "FAILED" and summary.connection_lost is True
    # the run aborted after the first connection failure: one instrument ERROR_RETRYABLE, the rest untouched
    statuses = [store.im_get_instrument(i).qualification_status for i in ids.values()]
    assert statuses.count("ERROR_RETRYABLE") == 1 and set(statuses) <= {"ERROR_RETRYABLE", "DISCOVERED"}


async def test_instrument_error_mentioning_connection_does_not_abort_run():
    """Regression (inverse): a per-instrument retryable error whose MESSAGE mentions 'IBKR connection' must
    NOT be mistaken for a global connection loss — isolation is preserved and the run completes."""
    store = _store()
    ids = {s: _upsert(store, s) for s in ["P1", "P2", "P3"]}
    client = ScriptedClient({"P1": ("verified", 1),
                             "P2": ("retry", "IBKR connection reset for this contract"),
                             "P3": ("verified", 3)})
    summary = await qualify_instruments(store, client, run_label="notabort",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "COMPLETED" and summary.connection_lost is False
    assert store.im_get_instrument(ids["P1"]).qualification_status == "VERIFIED"
    assert store.im_get_instrument(ids["P3"]).qualification_status == "VERIFIED"
    assert store.im_get_instrument(ids["P2"]).qualification_status == "ERROR_RETRYABLE"


async def test_resume_counters_are_accurate_no_double_count():
    """Regression: reprocessing an ERROR_RETRYABLE instrument on resume must not double-count. Counts are
    derived at finalize, so processed == distinct instruments touched."""
    store = _store()
    x = _upsert(store, "RX")
    y = _upsert(store, "RY")
    checksum = qualification_request_checksum("res2", None, False)
    store.iq_create_run(run_id="run-r2", request_checksum=checksum, run_label="res2", exchange=None,
                        batch_size=25, pause_seconds=1.0)
    store.iq_advance_run_status("run-r2", "PLANNED", "RUNNING")
    # X already recorded ERROR_RETRYABLE in the crashed pass; Y claimed then crashed (PENDING).
    store.iq_apply_outcome(x, run_id="run-r2", qualification_status="ERROR_RETRYABLE", reason="pacing",
                           event={"id": "run-r2-e1", "seq": 1, "instrument_id": x,
                                  "event_type": "QUALIFY_RESULT", "status": "ERROR_RETRYABLE"})
    store.iq_mark_pending(y, "run-r2")
    client = ScriptedClient({"RX": ("verified", 1), "RY": ("verified", 2)})
    summary = await qualify_instruments(store, client, run_label="res2",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.resumed is True
    assert summary.processed == 2 and summary.verified == 2 and summary.error_retryable == 0


async def test_attempts_count_recorded_outcomes_not_claims():
    """Regression: iq_mark_pending must NOT burn an attempt — attempts increment only on a recorded outcome,
    so a crash between claim and outcome does not over-escalate a retryable instrument."""
    store = _store()
    iid = _upsert(store, "AT")
    store.iq_create_run(run_id="run-at", request_checksum="sha256:at", run_label="at", exchange=None,
                        batch_size=1, pause_seconds=1.0)
    store.iq_advance_run_status("run-at", "PLANNED", "RUNNING")
    store.iq_mark_pending(iid, "run-at")
    store.iq_mark_pending(iid, "run-at")                    # two claims, no outcome
    assert store.im_get_instrument(iid).qualification_attempts == 0
    store.iq_apply_outcome(iid, run_id="run-at", qualification_status="ERROR_RETRYABLE", reason="x",
                           event={"id": "run-at-e1", "seq": 1, "instrument_id": iid,
                                  "event_type": "QUALIFY_RESULT", "status": "ERROR_RETRYABLE"})
    assert store.im_get_instrument(iid).qualification_attempts == 1


async def test_connection_outage_does_not_consume_retry_budget():
    """Regression: repeated IBKR outages (ConnectionUnavailableError) must NOT increment qualification_attempts,
    so a later genuine transient error still gets the full max_attempts budget before ERROR_PERMANENT."""
    store = _store()
    iid = _upsert(store, "OUT")
    down = ScriptedClient({"OUT": ("connection", "Connectivity between IB and TWS has been lost")})
    cfg = QualificationConfig(pause_seconds=0.0, max_attempts=2)
    for _ in range(3):                                       # three outages in a row
        await qualify_instruments(store, down, run_label="out", config=cfg, sleep=lambda _: _noop())
    row = store.im_get_instrument(iid)
    assert row.qualification_status == "ERROR_RETRYABLE" and row.qualification_attempts == 0  # budget intact

    # now the broker is back but the instrument hits genuine pacing errors → escalates only after max_attempts
    pacing = ScriptedClient({"OUT": "retry"})
    await qualify_instruments(store, pacing, run_label="out", config=cfg, sleep=lambda _: _noop())
    assert store.im_get_instrument(iid).qualification_status == "ERROR_RETRYABLE"   # attempt 1 of 2
    await qualify_instruments(store, pacing, run_label="out", config=cfg, sleep=lambda _: _noop())
    assert store.im_get_instrument(iid).qualification_status == "ERROR_PERMANENT"   # attempt 2 → escalate
    assert store.im_get_instrument(iid).qualification_attempts == 2


def test_market_is_never_both_completed_and_failed():
    store = _store()
    store.iq_create_run(run_id="run-m", request_checksum="sha256:m", run_label="m", exchange=None,
                        batch_size=1, pause_seconds=1.0)
    store.iq_advance_run_status("run-m", "PLANNED", "RUNNING")
    store.iq_record_market("run-m", market="M", market_status="COMPLETED")
    store.iq_record_market("run-m", market="M", market_status="FAILED")
    run = store.iq_get_run("run-m")
    import json as _json
    assert _json.loads(run.completed_markets_json) == [] and _json.loads(run.failed_markets_json) == ["M"]


def test_event_seq_continues_after_max_not_count():
    store = _store()
    store.iq_create_run(run_id="run-s", request_checksum="sha256:s", run_label="s", exchange=None,
                        batch_size=1, pause_seconds=1.0)
    store.iq_advance_run_status("run-s", "PLANNED", "RUNNING")
    for seq in (1, 2, 5):                                   # a gap at 3,4 (simulating rolled-back outcomes)
        store.iq_record_market("run-s", market="M", market_status="COMPLETED",
                               event={"id": f"run-s-e{seq}", "seq": seq, "market": "M",
                                      "event_type": "MARKET_OK"})
    assert store.iq_max_event_seq("run-s") == 5             # MAX, not COUNT (=3)


def test_reclaim_stale_running_qualification_run():
    store = _store()
    store.iq_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", exchange=None,
                        batch_size=10, pause_seconds=1.0)
    store.iq_advance_run_status("stale", "PLANNED", "RUNNING")
    with store.tx() as cur:
        store._exec(cur, "UPDATE instrument_qualification_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))
    reclaimed = store.iq_reclaim_stale_running("2020-01-01T00:00:00+00:00",
                                               failure_code="STALE", failure_reason="crashed")
    assert reclaimed == ["stale"]
    assert store.iq_get_run("stale").status == "FAILED"
    assert any(e.event_type == "RECLAIM" for e in store.iq_list_run_events("stale"))
