"""§ WP11 — OFFLINE simulation of the bounded re-qualification runner (examples/requalify_wp11.py).

Proves, on a temp SQLite store with a fake IB (no network, no broker): the runner touches EXACTLY the
rows of ONE prior run left in ERROR_RETRYABLE (never DISCOVERED/VERIFIED/other-run rows), writes the WP11
detail + returned-venue fields, sends only the START_API handshake (71) and contract-details requests (9),
never queries an unmapped derivative, aborts BEFORE writing when the tripwire or the wire allowlist fires
(row left QUALIFICATION_PENDING), refuses a selection that does not match --expect before connecting, and
does nothing at all in --dry-run. The runner is loaded by file path (examples/ is not a package).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.store import open_store

# The engine's real _build_contract needs ib_async.Contract. Decide via find_spec (which does NOT import),
# never via importorskip at collection time: importing the broker SDK into sys.modules for the whole
# session would make every import-graph gate (which purges only atp.*) see a "prohibited" module.
pytestmark = pytest.mark.skipif(importlib.util.find_spec("ib_async") is None, reason="ib_async not installed")


@pytest.fixture(autouse=True)
def _unload_broker_sdk_after_test():
    """The real _build_contract lazily imports ib_async during a test; drop whatever it loaded afterwards so
    the session's sys.modules stays SDK-free for the import-graph gates."""
    before = set(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in before and name.split(".")[0] in ("ib_async", "ibapi", "ib_insync"):
            del sys.modules[name]

_RUNNER = Path(__file__).resolve().parents[1] / "examples" / "requalify_wp11.py"


def _load():
    spec = importlib.util.spec_from_file_location("requalify_wp11_undertest", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ fake IB (wire-faithful)
class _Event:
    def __init__(self):
        self._subs = []

    def __iadd__(self, fn):
        self._subs.append(fn)
        return self

    def __isub__(self, fn):
        if fn in self._subs:
            self._subs.remove(fn)
        return self

    def emit(self, *a):
        for fn in list(self._subs):
            fn(*a)


class FakeClient:
    def __init__(self):
        self.connected = False
        self.connects = []
        self.sent = []                       # ids that REACH the client (i.e. passed the allowlist)

    async def connectAsync(self, host, port, clientId, timeout=2.0):
        self.connects.append((host, port, clientId))
        self.connected = True
        self.send(71, 2, clientId, "")       # handshake goes through the (guarded) send attribute

    def send(self, *fields, **kw):
        self.sent.append(fields[0])

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False


class FakeIB:
    def __init__(self, script, wire_id=9):
        self.client = FakeClient()
        self.errorEvent = _Event()
        self.script = script
        self.calls = []
        self.wire_id = wire_id

    def isConnected(self):
        return self.client.isConnected()

    def disconnect(self):
        self.client.disconnect()

    async def reqContractDetailsAsync(self, contract):
        self.client.send(self.wire_id, 8, 1, contract)     # through the guarded attribute
        key = getattr(contract, "secId", "") or getattr(contract, "symbol", "")
        self.calls.append(key)
        b = self.script.get(key)
        if b == "nosecdef":
            self.errorEvent.emit(1, 200, "No security definition has been found for the request")
            return []
        if b == "trip":
            self.errorEvent.emit(1, 1100, "Connectivity between IB and TWS has been lost")
            return []
        return b or []

    def placeOrder(self, *a, **k):
        raise AssertionError("placeOrder must never be called")

    def reqMktData(self, *a, **k):
        raise AssertionError("reqMktData must never be called")

    def reqPositions(self, *a, **k):
        raise AssertionError("reqPositions must never be called")

    def reqAccountUpdates(self, *a, **k):
        raise AssertionError("reqAccountUpdates must never be called")


def _detail(con_id, *, isin="", primary="SBF", exchange="SMART", currency="EUR", sec_type="STK"):
    sec_id_list = [SimpleNamespace(tag="ISIN", value=isin)] if isin else []
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol="X", localSymbol="X", secType=sec_type,
                                 exchange=exchange, primaryExchange=primary, currency=currency,
                                 lastTradeDateOrContractMonth="", strike=0.0, right="", multiplier="1",
                                 underConId=0),
        secIdList=sec_id_list, longName="X", minTick=0.01, stockType="", country="FR")


# ------------------------------------------------------------------ store seeding (the WP11 backlog)
_FUT = {"expiry": "20261218"}
_OPT = {"expiry": "20261218", "strike": "100", "option_right": "C"}
CASH = [  # 11 cash lines on the canary's MTF/SI MICs (all unmapped → registry fail-closed)
    (AssetClass.EQUITY, "AQEU", "FR0000000101"), (AssetClass.EQUITY, "AACA", "FR0000000102"),
    (AssetClass.EQUITY, "BBIS", "IE0000000103"), (AssetClass.ETF, "BTFE", "NL0000000104"),
    (AssetClass.ETF, "AQEA", "FR0000000105"), (AssetClass.ETF, "BETA", "HU0000000106"),
    (AssetClass.FUND, "AQED", "FR0000000107"), (AssetClass.FUND, "BGEM", "IT0000000108"),
    (AssetClass.FUND, "BEUP", "NL0000000109"), (AssetClass.WARRANT, "DUSD", "DE0000000110"),
    (AssetClass.WARRANT, "DUSB", "DE0000000111"),
]
DERIV = [  # 6 derivatives on unmapped derivatives-segment MICs (never queried)
    (AssetClass.FUTURE, "D2XC", "NL0000000201", _FUT), (AssetClass.FUTURE, "EUWB", "SE0000000202", _FUT),
    (AssetClass.FUTURE, "DKFI", "SE0000000203", _FUT), (AssetClass.OPTION, "XBRD", "BE0000000204", _OPT),
    (AssetClass.OPTION, "NOED", "SE0000000205", _OPT), (AssetClass.OPTION, "DKED", "SE0000000206", _OPT),
]
ECHO = {"FR0000000101", "FR0000000102", "IE0000000103", "NL0000000104", "FR0000000105", "HU0000000106"}
NOECHO = {"FR0000000107", "IT0000000108", "NL0000000109"}
CCY_CONFLICT, NOT_FOUND = "DE0000000110", "DE0000000111"


def _rec(ac, mic, isin, extra=None):
    kw = dict(symbol=isin, asset_class=ac, exchange=mic, trading_currency="EUR", isin=isin, local_symbol=isin,
              primary_exchange=mic, region="EUROPE", country="FR", timezone="Europe/Paris",
              trading_calendar="eu", source="t")
    kw.update(extra or {"multiplier": "1"})
    return InstrumentRecord(**kw)


def _ev(run_id, seq, iid):
    return {"id": f"{run_id}-e{seq}", "seq": seq, "instrument_id": iid, "event_type": "QUALIFY_RESULT",
            "severity": "ERROR"}


def _seed_backlog(store, run_id, specs):
    """Put `specs` into ERROR_RETRYABLE under `run_id`, exactly as a prior canary would have left them."""
    store.iq_create_run(run_id=run_id, request_checksum="c", run_label="seed", exchange=None, batch_size=1,
                        pause_seconds=0.0)
    store.iq_advance_run_status(run_id, "PLANNED", "RUNNING")
    ids = []
    for seq, spec in enumerate(specs, start=1):
        rec = _rec(*spec)
        store.im_upsert_instrument(rec.as_record())
        store.iq_mark_pending(rec.instrument_id, run_id)
        store.iq_apply_outcome(rec.instrument_id, run_id=run_id, qualification_status="ERROR_RETRYABLE",
                               reason="venue_unresolved: seed", count_attempt=False,
                               event=_ev(run_id, seq, rec.instrument_id))
        ids.append(rec.instrument_id)
    store.iq_finalize_run(run_id, status="COMPLETED")
    return ids


def _setup(mod):
    path = str(Path(tempfile.mkdtemp()) / "atp.db")
    store = open_store(path)                                   # migrations 1..32 applied here
    targets = _seed_backlog(store, mod.SOURCE_RUN_ID, CASH + DERIV)
    # decoys that must NEVER be touched: a fresh DISCOVERED row, an ERROR_RETRYABLE row of ANOTHER run,
    # and a VERIFIED row of the source run
    disc = _rec(AssetClass.EQUITY, "XPAR", "FR0000000901")
    store.im_upsert_instrument(disc.as_record())
    other = _seed_backlog(store, "otherrun00000000000000000000000",
                          [(AssetClass.EQUITY, "AQEU", "FR0000000902")])
    ver = _rec(AssetClass.EQUITY, "XPAR", "FR0000000903")
    store.im_upsert_instrument(ver.as_record())
    store.iq_create_run(run_id="verrun", request_checksum="c", run_label="seed2", exchange=None, batch_size=1,
                        pause_seconds=0.0)
    store.iq_advance_run_status("verrun", "PLANNED", "RUNNING")
    store.iq_mark_pending(ver.instrument_id, "verrun")
    store.iq_apply_outcome(ver.instrument_id, run_id="verrun", qualification_status="VERIFIED", reason="ok",
                           verification_status="verified", con_id=555, set_last_verified=True,
                           event=_ev("verrun", 1, ver.instrument_id))
    store.iq_finalize_run("verrun", status="COMPLETED")
    decoys = {"disc": disc.instrument_id, "other": other[0], "ver": ver.instrument_id}
    return path, store, targets, decoys


def _script():
    s = {isin: [_detail(9000 + int(isin[-3:]), isin=isin, primary="SBF")] for isin in ECHO}
    s.update({isin: [_detail(9000 + int(isin[-3:]), isin="", primary="SBF")] for isin in NOECHO})
    s[CCY_CONFLICT] = [_detail(9110, isin=CCY_CONFLICT, primary="SBF", currency="USD")]
    s[NOT_FOUND] = "nosecdef"
    return s


def _ns(mod, store_path, **over):
    d = dict(host="127.0.0.1", port=4002, client_id=9204, store_url=store_path, source_run=mod.SOURCE_RUN_ID,
             expect=17, max=17, pause=0.0, request_timeout=None, connect_timeout=1.0, dry_run=False,
             instrument_ids="")
    d.update(over)
    return argparse.Namespace(**d)


def _runs(store, label):
    return [r for r in store.iq_list_runs(limit=50) if r.run_label == label]


# ------------------------------------------------------------------ happy path: the documented §8 pass
def test_bounded_pass_requalifies_exactly_the_backlog_with_wp11_fields():
    mod = _load()
    path, store, targets, decoys = _setup(mod)
    fake = FakeIB(_script())
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 0
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "COMPLETED" and run.processed_count == 17
    assert (run.verified_count, run.error_retryable_count, run.not_tradable_count) == (6, 10, 1)
    by_isin = {store.im_get_instrument(i).isin: store.im_get_instrument(i) for i in targets}
    for isin in ECHO:                                          # ISIN echo + real venue → VERIFIED
        r = by_isin[isin]
        assert r.qualification_status == "VERIFIED" and r.con_id == 9000 + int(isin[-3:])
        assert r.qualification_detail == "verified_isin_echo" and r.ibkr_primary_exchange == "SBF"
        assert r.exchange == r.primary_exchange and r.exchange not in ("SBF", "SMART")   # MIC preserved
    for isin in NOECHO:                                        # no echo + unmapped MIC → never verified
        r = by_isin[isin]
        assert r.qualification_status == "ERROR_RETRYABLE" and r.con_id is None
        assert r.qualification_detail == "venue_unresolved"
    assert by_isin[CCY_CONFLICT].qualification_detail == "currency_conflict"
    assert by_isin[NOT_FOUND].qualification_status == "NOT_TRADABLE"
    for _ac, _mic, isin, _x in DERIV:                          # derivatives: no query, still re-queryable
        r = by_isin[isin]
        assert r.qualification_status == "ERROR_RETRYABLE" and r.qualification_detail == "venue_unresolved"
        assert isin not in fake.calls
    assert all(store.im_get_instrument(i).qualification_run_id == run.run_id for i in targets)
    assert set(fake.calls) == {c[2] for c in CASH} and len(fake.calls) == 11   # each cash line once
    assert fake.client.sent[0] == 71 and set(fake.client.sent[1:]) == {9} and len(fake.client.sent) == 12
    assert fake.client.connects == [("127.0.0.1", 4002, 9204)]
    d = store.im_get_instrument(decoys["disc"])
    assert d.qualification_status == "DISCOVERED" and d.qualification_run_id is None
    o = store.im_get_instrument(decoys["other"])
    assert o.qualification_status == "ERROR_RETRYABLE" and o.qualification_run_id.startswith("otherrun")
    v = store.im_get_instrument(decoys["ver"])
    assert v.qualification_status == "VERIFIED" and v.con_id == 555 and v.qualification_run_id == "verrun"


# ------------------------------------------------------------------ guards abort BEFORE writing
def test_tripwire_aborts_before_write_and_leaves_row_pending():
    mod = _load()
    path, store, targets, _ = _setup(mod)
    order = mod.select_targets(store, source_run_id=mod.SOURCE_RUN_ID, expect=17, max_rows=17)
    script = _script()
    script[order[2].isin] = "trip"                              # non-benign 1100 during the 3rd request
    fake = FakeIB(script)
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "FAILED" and run.failure_code == "ABORTED"
    hit = store.im_get_instrument(order[2].instrument_id)
    assert hit.qualification_status == "QUALIFICATION_PENDING"   # outcome NOT written → re-selectable
    assert hit.qualification_detail is None and hit.con_id is None
    for r in order[:2]:
        assert store.im_get_instrument(r.instrument_id).qualification_run_id == run.run_id
    for r in order[3:]:                                          # nothing after the abort was touched
        row = store.im_get_instrument(r.instrument_id)
        assert row.qualification_status == "ERROR_RETRYABLE"
        assert row.qualification_run_id == mod.SOURCE_RUN_ID
    assert fake.calls == [r.isin for r in order[:3]]


def test_wire_allowlist_refuses_any_other_message_and_aborts():
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    order = mod.select_targets(store, source_run_id=mod.SOURCE_RUN_ID, expect=17, max_rows=17)
    fake = FakeIB(_script(), wire_id=6)                          # an account-updates style message id
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    assert fake.client.sent == [71]                              # 6 was refused before reaching the client
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "FAILED" and "violation=6" in (run.failure_reason or "")
    assert store.im_get_instrument(order[0].instrument_id).qualification_status == "QUALIFICATION_PENDING"
    for r in order[1:]:
        assert store.im_get_instrument(r.instrument_id).qualification_run_id == mod.SOURCE_RUN_ID


# ------------------------------------------------------------------ fail-closed selection / dry-run
def test_selection_mismatch_refuses_before_connecting():
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    called = []
    mod.make_ib = lambda: called.append(1) or FakeIB({})
    rc = asyncio.run(mod.main(_ns(mod, path, expect=16)))
    assert rc == 2 and called == []                              # refused; no IB ever built
    assert _runs(store, mod.RUN_LABEL) == []                     # no run row created


def test_dry_run_touches_nothing():
    mod = _load()
    path, store, targets, _ = _setup(mod)
    called = []
    mod.make_ib = lambda: called.append(1) or FakeIB({})
    rc = asyncio.run(mod.main(_ns(mod, path, dry_run=True)))
    assert rc == 0 and called == [] and _runs(store, mod.RUN_LABEL) == []
    assert all(store.im_get_instrument(i).qualification_run_id == mod.SOURCE_RUN_ID for i in targets)


# ------------------------------------------------------------------ surface guards
def test_runner_source_has_no_order_marketdata_or_account_requests():
    src = _RUNNER.read_text()
    for forbidden in ("placeOrder", "cancelOrder", "modifyOrder", "reqMktData", "reqPositions",
                      "reqAccountUpdates", "reqAccountSummary", "reqOpenOrders", "reqAllOpenOrders",
                      "reqExecutions", "reqScannerSubscription", "reqMktDepth", "reqRealTimeBars",
                      "reqHistoricalData", "place_order", "cancel_order"):
        assert forbidden not in src, f"{forbidden} must not appear in the runner"
    assert "ib.client.connectAsync" in src and "migrate=False" in src and "--dry-run" in src


# ------------------------------------------------------------------ review fixes (review of 691e72d)
class _TamperingStore:
    """Delegates to the real store but returns a TAMPERED row on the first im_get_instrument read (the
    runner's checkpoint read-back) — simulating persisted values that do not match what the engine
    reported."""

    def __init__(self, real, tamper):
        self._real, self._tamper, self._done = real, tamper, False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def im_get_instrument(self, iid):
        row = self._real.im_get_instrument(iid)
        if not self._done:
            self._done = True
            return self._tamper(row)
        return row


def test_checkpoint_persisted_mismatch_aborts_before_rest():
    import dataclasses
    mod = _load()
    path, store, targets, _ = _setup(mod)
    order = mod.select_targets(store, source_run_id=mod.SOURCE_RUN_ID, expect=17, max_rows=17)
    real_open = mod.open_store
    mod.open_store = lambda url, migrate=False: _TamperingStore(
        real_open(url, migrate=migrate),
        lambda row: dataclasses.replace(row, qualification_run_id="tampered"))
    fake = FakeIB(_script())
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "FAILED" and "checkpoint verification failed" in (run.failure_reason or "")
    assert "run_id=" in run.failure_reason
    assert fake.calls == [order[0].isin]                          # the REST phase never started
    cp = store.im_get_instrument(order[0].instrument_id)          # checkpoint itself was persisted normally
    assert cp.qualification_run_id == run.run_id and cp.qualification_status != "QUALIFICATION_PENDING"
    for r in order[1:]:                                           # nothing after it was touched
        row = store.im_get_instrument(r.instrument_id)
        assert row.qualification_status == "ERROR_RETRYABLE" and row.qualification_run_id == mod.SOURCE_RUN_ID


def test_verify_checkpoint_requires_exact_equality_with_engine_outcome():
    mod = _load()
    orig = SimpleNamespace(exchange="AFSO", primary_exchange="AFSO")
    exp = {"status": "VERIFIED", "con_id": 9101, "detail": "verified_isin_echo",
           "ibkr_primary_exchange": "SBF", "reason": "unique contract match"}
    row = dict(qualification_run_id="run1", exchange="AFSO", primary_exchange="AFSO",
               qualification_status="VERIFIED", con_id=9101, qualification_detail="verified_isin_echo",
               ibkr_primary_exchange="SBF", qualification_reason="unique contract match")

    def check(changes=None, run_id="run1", expected=exp):
        return mod.verify_checkpoint(SimpleNamespace(**{**row, **(changes or {})}), run_id=run_id,
                                     expected=expected, original=orig)

    assert check() is None
    # foreign-but-PLAUSIBLE values are mismatches, not merely missing ones
    assert "con_id=9999 != expected 9101" in check({"con_id": 9999})
    assert "ibkr_primary_exchange='IBIS' != expected 'SBF'" in check({"ibkr_primary_exchange": "IBIS"})
    wrong_detail = check({"qualification_detail": "verified_venue_match"})
    assert "qualification_detail='verified_venue_match'" in wrong_detail
    assert "qualification_reason=" in check({"qualification_reason": "other"})
    assert "run_id=" in check(run_id="other")
    assert "qualification_status=" in check({"qualification_status": "AMBIGUOUS"})
    assert "venue fields were modified" in check({"exchange": "SBF"})
    pend_exp = {**exp, "status": "QUALIFICATION_PENDING", "con_id": None}
    assert "QUALIFICATION_PENDING" in check({"qualification_status": "QUALIFICATION_PENDING", "con_id": None},
                                            expected=pend_exp)
    # a non-VERIFIED outcome must match exactly too (None == None); an insane VERIFIED expectation is flagged
    exp2 = {"status": "ERROR_RETRYABLE", "con_id": None, "detail": "venue_unresolved",
            "ibkr_primary_exchange": None, "reason": "venue_unresolved: x"}
    row2 = {"qualification_status": "ERROR_RETRYABLE", "con_id": None,
            "qualification_detail": "venue_unresolved", "ibkr_primary_exchange": None,
            "qualification_reason": "venue_unresolved: x"}
    assert check(row2, expected=exp2) is None
    assert "con_id=5 != expected None" in check({**row2, "con_id": 5}, expected=exp2)
    assert "without a real returned venue" in check({"ibkr_primary_exchange": "SMART"},
                                                    expected={**exp, "ibkr_primary_exchange": "SMART"})
    missing = mod.verify_checkpoint(None, run_id="run1", expected=exp, original=orig)
    assert missing.endswith("checkpoint row missing from the store")


def test_sdk_construction_failure_finalizes_run_failed_not_running():
    mod = _load()
    path, store, targets, _ = _setup(mod)

    def boom():
        raise RuntimeError("SDK unavailable")

    mod.make_ib = boom
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    runs = _runs(store, mod.RUN_LABEL)
    assert len(runs) == 1 and runs[0].status == "FAILED" and "SDK unavailable" in runs[0].failure_reason
    assert store.iq_list_runs(status="RUNNING", limit=50) == []       # never left RUNNING
    for i in targets:                                                  # nothing touched
        row = store.im_get_instrument(i)
        assert row.qualification_status == "ERROR_RETRYABLE" and row.qualification_run_id == mod.SOURCE_RUN_ID


def test_documented_pause_between_checkpoint_and_rest_and_between_rows():
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    sleeps = []

    async def rec(s):
        sleeps.append(s)

    mod._sleep = rec
    fake = FakeIB(_script())
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path, pause=0.5)))
    assert rc == 0
    assert sleeps == [0.5] * 16          # 1 after the checkpoint + 15 between the 16 rest rows (17 rows)


class _TripAfterIB(FakeIB):
    """Returns row `trip_after` normally, then raises an UNSOLICITED non-benign error on the next loop tick —
    i.e. between requests, during the documented pause."""

    def __init__(self, script, trip_after):
        super().__init__(script)
        self.trip_after = trip_after

    async def reqContractDetailsAsync(self, contract):
        details = await super().reqContractDetailsAsync(contract)
        key = getattr(contract, "secId", "") or getattr(contract, "symbol", "")
        if key == self.trip_after:
            asyncio.get_running_loop().call_soon(self.errorEvent.emit, -1, 1100,
                                                 "Connectivity between IB and TWS has been lost")
        return details


def test_tripwire_between_requests_leaves_next_row_untouched():
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    order = mod.select_targets(store, source_run_id=mod.SOURCE_RUN_ID, expect=17, max_rows=17)
    fake = _TripAfterIB(_script(), trip_after=order[1].isin)     # first REST row completes, then the trip
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "FAILED" and "tripwire before request" in (run.failure_reason or "")
    assert fake.calls == [order[0].isin, order[1].isin]
    done = store.im_get_instrument(order[1].instrument_id)
    assert done.qualification_run_id == run.run_id and done.qualification_status != "QUALIFICATION_PENDING"
    nxt = store.im_get_instrument(order[2].instrument_id)            # pre-request check: untouched
    assert nxt.qualification_status == "ERROR_RETRYABLE" and nxt.qualification_run_id == mod.SOURCE_RUN_ID


def test_runner_describes_broker_readonly_and_store_writes_honestly():
    src = _RUNNER.read_text()
    assert "Broker side: READ-ONLY" in src and "WRITES qualification outcomes" in src
    assert "READ-ONLY IBKR re-qualification" not in src                # whole runner is not read-only



# ------------------------------------------------------------------ review of 84a2081 (equality, ids)
_SEC_TYPE = {AssetClass.EQUITY: "STK", AssetClass.ETF: "STK", AssetClass.FUND: "FUND",
             AssetClass.WARRANT: "WAR"}


def _script_all_echo():
    """Every cash line echoes its ISIN with a class-compatible secType → the checkpoint (first cash row)
    is deterministically VERIFIED."""
    return {c[2]: [_detail(9000 + int(c[2][-3:]), isin=c[2], primary="SBF", sec_type=_SEC_TYPE[c[0]])]
            for c in CASH}


@pytest.mark.parametrize("field,value,token", [
    ("con_id", 9999, "con_id=9999 != expected"),                 # foreign but plausible conId
    ("ibkr_primary_exchange", "IBIS", "ibkr_primary_exchange='IBIS' != expected 'SBF'"),   # real venue, wrong
    ("qualification_detail", "verified_venue_match", "qualification_detail='verified_venue_match'"),
])
def test_checkpoint_foreign_plausible_value_aborts_before_rest(field, value, token):
    import dataclasses
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    order = mod.select_targets(store, source_run_id=mod.SOURCE_RUN_ID, expect=17, max_rows=17)
    real_open = mod.open_store
    mod.open_store = lambda url, migrate=False: _TamperingStore(
        real_open(url, migrate=migrate), lambda row: dataclasses.replace(row, **{field: value}))
    fake = FakeIB(_script_all_echo())
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path)))
    assert rc == 1
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "FAILED" and token in (run.failure_reason or "")
    assert fake.calls == [order[0].isin]                          # REST never started
    cp = store.im_get_instrument(order[0].instrument_id)          # the real row holds the engine's values
    assert cp.qualification_status == "VERIFIED" and cp.con_id == 9000 + int(order[0].isin[-3:])
    assert cp.ibkr_primary_exchange == "SBF" and cp.qualification_detail == "verified_isin_echo"
    for r in order[1:]:
        row = store.im_get_instrument(r.instrument_id)
        assert row.qualification_status == "ERROR_RETRYABLE" and row.qualification_run_id == mod.SOURCE_RUN_ID


BONDS = [(AssetClass.BOND, "AURO", "FR001400Q3S1"), (AssetClass.BOND, "AFSO", "NL000000AFS2"),
         (AssetClass.BOND, "ALXP", "FR000000ALX3")]


def _seed_reset_bonds(store):
    """The 3 bonds AFTER infra/db/wp11_bond_reset.sql: DISCOVERED, run id NULL, con_id NULL."""
    ids = []
    for spec in BONDS:
        rec = _rec(*spec)
        store.im_upsert_instrument(rec.as_record())
        ids.append(rec.instrument_id)
    return ids


def test_explicit_ids_requalify_exactly_the_three_reset_bonds():
    mod = _load()
    path, store, targets, decoys = _setup(mod)             # the 17 + decoys are present, must stay untouched
    bond_ids = _seed_reset_bonds(store)
    script = {"FR001400Q3S1": [_detail(7001, isin="FR001400Q3S1", primary="SBF", sec_type="BOND")],
              "NL000000AFS2": "nosecdef", "FR000000ALX3": "nosecdef"}
    fake = FakeIB(script)
    mod.make_ib = lambda: fake
    rc = asyncio.run(mod.main(_ns(mod, path, instrument_ids=",".join(bond_ids), expect=3, max=3)))
    assert rc == 0
    run = _runs(store, mod.RUN_LABEL)[0]
    assert run.status == "COMPLETED" and run.processed_count == 3
    assert (run.verified_count, run.error_retryable_count, run.not_tradable_count) == (1, 2, 0)
    by_isin = {store.im_get_instrument(i).isin: store.im_get_instrument(i) for i in bond_ids}
    v = by_isin["FR001400Q3S1"]
    assert v.qualification_status == "VERIFIED" and v.con_id == 7001 and v.ibkr_primary_exchange == "SBF"
    for isin in ("NL000000AFS2", "FR000000ALX3"):                  # bond not-found is NOT terminal
        assert by_isin[isin].qualification_status == "ERROR_RETRYABLE"
        assert by_isin[isin].qualification_detail == "bond_not_found"
    assert all(store.im_get_instrument(i).qualification_run_id == run.run_id for i in bond_ids)
    assert sorted(fake.calls) == sorted(b[2] for b in BONDS)      # bonds queried by ISIN-in-symbol, once each
    for i in targets:                                            # the 17 are NOT widened into this run
        row = store.im_get_instrument(i)
        assert row.qualification_status == "ERROR_RETRYABLE" and row.qualification_run_id == mod.SOURCE_RUN_ID
    d = store.im_get_instrument(decoys["disc"])                  # other DISCOVERED rows untouched
    assert d.qualification_status == "DISCOVERED" and d.qualification_run_id is None


def test_explicit_ids_refuse_unknown_non_selectable_or_miscounted_ids():
    mod = _load()
    path, store, _targets, decoys = _setup(mod)
    bond_ids = _seed_reset_bonds(store)
    called = []
    mod.make_ib = lambda: called.append(1) or FakeIB({})
    # (a) an id that is VERIFIED (carries a con_id) → not re-selectable
    ids_with_verified = ",".join(bond_ids[:2] + [decoys["ver"]])
    rc = asyncio.run(mod.main(_ns(mod, path, instrument_ids=ids_with_verified, expect=3)))
    assert rc == 2
    # (b) an unknown id
    rc = asyncio.run(mod.main(_ns(mod, path, instrument_ids=",".join(bond_ids[:2] + ["INS-doesnotexist"]),
                                  expect=3)))
    assert rc == 2
    # (c) --expect does not match the number of ids (the run-mode default 17 must never leak in)
    rc = asyncio.run(mod.main(_ns(mod, path, instrument_ids=",".join(bond_ids), expect=17)))
    assert rc == 2
    assert called == [] and _runs(store, mod.RUN_LABEL) == []   # refused before any IB or run row
    for i in bond_ids:
        assert store.im_get_instrument(i).qualification_status == "DISCOVERED"


def test_run_mode_cannot_see_reset_bonds_and_explicit_mode_does_not_widen():
    # The honest reason for explicit-ID mode: after the reset the bonds are DISCOVERED with a NULL run id, so
    # run mode (--expect 3 against any run id) finds 0 and refuses — it must NOT silently include them.
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    _seed_reset_bonds(store)
    called = []
    mod.make_ib = lambda: called.append(1) or FakeIB({})
    rc = asyncio.run(mod.main(_ns(mod, path, source_run="newrun000", expect=3)))
    assert rc == 2 and called == []
    rc = asyncio.run(mod.main(_ns(mod, path, expect=17, dry_run=True)))   # the 17-selector is unchanged
    assert rc == 0



def test_explicit_duplicate_ids_are_refused_not_deduplicated():
    mod = _load()
    path, store, _targets, _ = _setup(mod)
    a, b, _c = _seed_reset_bonds(store)
    called = []
    mod.make_ib = lambda: called.append(1) or FakeIB({})
    for ids, expect in ((f"{a},{a},{b}", 3), (f"{a},{a},{b}", 2), (f"{a}, {a}", 1)):
        rc = asyncio.run(mod.main(_ns(mod, path, instrument_ids=ids, expect=expect)))
        assert rc == 2                                            # refused, never silently de-duplicated
    assert called == [] and _runs(store, mod.RUN_LABEL) == []
    for i in (a, b):
        assert store.im_get_instrument(i).qualification_status == "DISCOVERED"
