#!/usr/bin/env python3
"""Bounded IBKR re-qualification of the WP11 backlog — RUN ONLY UNDER EXPLICIT AUTHORIZATION.

Broker side: READ-ONLY (only contract-details requests). Store side: WRITES qualification outcomes for the
selected rows (status / reason / detail / returned venue / con_id) plus one run row and its audit events —
exactly what the qualification engine writes, bounded to the selection below. Nothing else is written.

Implements the execution step of docs/WP11_canonical_venue_identity.md §8: re-qualify EXACTLY the
instruments a previous qualification run left in the re-selectable ``ERROR_RETRYABLE`` state (by default
the second WP10 canary, run ``cb7a8800…``: 11 cash + 6 derivatives = 17 rows, ``con_id`` NULL) against the
deployed WP11 code — so the first safe ``VERIFIED`` results can be produced with NO reset and without
touching the other 4,780 ``DISCOVERED`` rows.

Why not ``qualify_instruments()`` directly: it selects EVERY re-selectable row (``DISCOVERED`` included)
and cannot be bounded to one prior run. This runner reuses the engine's per-instrument path
(``_qualify_one`` → ``_outcome_fields`` → conId-collision guard → ``iq_apply_outcome`` with the WP11
``qualification_detail`` / ``ibkr_primary_exchange`` fields) under a hard, fail-closed selection.

Guarantees (mirrors the WP10 canary runner that produced runs ``68a330fb…`` and ``cb7a8800…``):
  * Selection: ``ERROR_RETRYABLE`` AND ``qualification_run_id == --source-run`` AND ``con_id IS NULL``,
    cash first; the count MUST equal ``--expect`` or the runner refuses BEFORE opening any connection.
  * Connection: the low-level ``ib.client.connectAsync`` handshake only (the high-level
    ``IB().connectAsync`` additionally subscribes to positions, account updates and executions — never
    issued here).
  * Wire allowlist: ``ib.client.send`` is wrapped; only msg 71 (START_API handshake) and msg 9 (contract
    details) may leave the process — anything else is refused before it is sent and aborts the run.
  * Tripwire: any non-benign IBKR error event aborts. It is checked BEFORE every request (a trip between
    requests leaves the next row completely untouched) and AFTER every request (a trip during a request
    leaves that row ``QUALIFICATION_PENDING`` — its outcome is NOT written, so it stays re-selectable).
  * Two phases: one checkpoint instrument whose PERSISTED values are read back and verified (run id,
    status, con_id / detail / venue consistency, FIRDS MIC untouched) — any deviation aborts BEFORE the
    rest; then the documented pause; then the rest. Fixed pause and timeouts; no retries; abort on
    disconnect / real error / entitlement problem.
  * Budget-neutral WP11 outcomes (``venue_unresolved`` / ``currency_conflict`` / ``bond_not_found``) never
    abort — they are the expected, honest results for a still-unmapped venue.
  * Run lifecycle: once the run row is RUNNING, every failure — including SDK construction or the
    handshake — is caught and the run is finalized FAILED (never left RUNNING); the socket is closed.
  * ``--dry-run`` prints the exact selection and touches nothing (no connection, no write).
  * The store is opened with ``migrate=False``: a not-yet-deployed migration fails loudly instead of being
    applied by an ops script (deploy first — migration 032 applies at service start).

SAFETY: no orders, no market data, no account, position, scanner or subscription request.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0. CI never runs this against a broker; the offline
simulation lives in tests/test_requalify_wp11.py.

Usage (on the host, as the service user, env from atp.env — each run needs its own authorization):
    PYTHONPATH=src python3 examples/requalify_wp11.py --dry-run
    PYTHONPATH=src python3 examples/requalify_wp11.py --port 4002 --client-id 9204 --expect 17
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import uuid
from typing import Any

from atp.instruments.qualification import (
    IbkrQualificationClient,
    QualificationStatus,
    _outcome_fields,
    _qualification_detail,
    _qualify_one,
    _venue_of_record,
    qualification_request_checksum,
)
from atp.store import open_store

SOURCE_RUN_ID = "cb7a88002d074b60862eea807dc2ab8e"   # second WP10 canary (docs/WP11 §8)
RUN_LABEL = "ibkr_requalify_wp11"
DEFAULT_EXPECT = 17
DEFAULT_CLIENT_ID = 9204
PAUSE_S = 3.0
REQUEST_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 15.0
MAX_ATTEMPTS = 3
ALLOWED_WIRE_IDS = frozenset({71, 9})            # START_API handshake + contract details — nothing else
BENIGN_ERROR_CODES = frozenset({200, 2104, 2106, 2107, 2108, 2119, 2158})   # per-request 200 + farm info
_DERIVATIVES = frozenset({"future", "option"})
_ABORT_STATUSES = frozenset({QualificationStatus.ERROR_RETRYABLE.value,
                             QualificationStatus.ERROR_PERMANENT.value})
_VERIFIED_DETAILS = frozenset({"verified_isin_echo", "verified_venue_match"})
_ROUTING_TOKENS = frozenset({"", "SMART", "SMARTUS", "IBKRATS", "OVERNIGHT", "VALUE", "DARKPOOL"})

_sleep = asyncio.sleep     # indirection so the offline simulation can observe the documented pauses


class SelectionMismatch(RuntimeError):
    """The bounded selection did not match ``--expect`` — refuse before any connection or write."""


def resolve_store_url(explicit: str | None) -> str:
    """``--store-url`` wins; else the service env (mirrors atp.services.base.build_dsn). Never a default."""
    if explicit:
        return explicit
    for key in ("ATP_DATABASE_URL", "ATP_STORE_URL", "DATABASE_URL"):
        if os.environ.get(key):
            return os.environ[key]
    pw = os.environ.get("ATP_APP_PASSWORD")
    if not pw:
        raise SystemExit("no store URL: pass --store-url or set ATP_DATABASE_URL (or the atp.env app creds)")
    user = os.environ.get("ATP_APP_USER", "atp_app")
    db = os.environ.get("ATP_PROD_DB", "atp_prod")
    host = os.environ.get("ATP_PG_HOST", "127.0.0.1")
    port = os.environ.get("ATP_PG_PORT", "5432")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def is_derivative(row: Any) -> bool:
    return (getattr(row, "asset_class", "") or "").strip().lower() in _DERIVATIVES


def select_targets(store: Any, *, source_run_id: str, expect: int, max_rows: int) -> list:
    """The bounded selection: re-selectable rows of ONE prior run, con_id NULL, cash first. Exactly
    ``expect`` rows or SelectionMismatch (fail-closed: never widen, never guess)."""
    rows = store.iq_select_instruments(statuses=[QualificationStatus.ERROR_RETRYABLE.value], limit=5000)
    rows = [r for r in rows if r.qualification_run_id == source_run_id and r.con_id is None]
    rows.sort(key=lambda r: (is_derivative(r), r.instrument_id))
    if len(rows) != expect:
        raise SelectionMismatch(f"selection is {len(rows)} row(s), expected exactly {expect} for run "
                                f"{source_run_id} — refusing (nothing connected, nothing written)")
    return rows[:max_rows]


def new_guard() -> dict:
    return {"sent_ids": [], "violation": None, "errors": [], "error_abort": None}


def guard_tripped(guard: dict) -> bool:
    return guard["violation"] is not None or guard["error_abort"] is not None


def make_ib() -> Any:
    import ib_async  # lazy: the module loads (and is simulated) without the broker SDK
    return ib_async.IB()


def install_guards(ib: Any, guard: dict) -> None:
    """Wire allowlist on ``ib.client.send`` + error-event tripwire. Installed BEFORE the handshake."""
    client = ib.client
    original_send = client.send

    def guarded_send(*fields: Any, **kw: Any) -> Any:
        msg_id = fields[0] if fields else None
        guard["sent_ids"].append(msg_id)
        if msg_id not in ALLOWED_WIRE_IDS:
            guard["violation"] = msg_id
            raise RuntimeError(f"wire allowlist violation: msg id {msg_id!r} refused "
                               f"(only {sorted(ALLOWED_WIRE_IDS)} permitted)")
        return original_send(*fields, **kw)

    client.send = guarded_send

    def on_error(req_id: Any, code: Any, message: Any = "", *_rest: Any) -> None:
        try:
            code_i: int | None = int(code)
        except (TypeError, ValueError):
            code_i = None
        guard["errors"].append(code_i)
        if code_i not in BENIGN_ERROR_CODES:
            guard["error_abort"] = code_i

    ib.errorEvent += on_error


async def connect(ib: Any, host: str, port: int, client_id: int, timeout: float) -> None:
    """Handshake only (msg 71). Deliberately NOT the high-level connect, which also subscribes to
    positions / account updates / executions."""
    await ib.client.connectAsync(host, port, client_id, timeout)


def verify_checkpoint(persisted: Any, *, run_id: str, expected_status: str, original: Any) -> str | None:
    """Compare the PERSISTED checkpoint row with what the engine reported. Returns None when consistent,
    else a reason — any deviation must abort before the rest of the selection is processed."""
    problems: list[str] = []
    if persisted is None:
        return "checkpoint row missing from the store"
    if persisted.qualification_run_id != run_id:
        problems.append(f"run_id={persisted.qualification_run_id!r} != {run_id!r}")
    if persisted.qualification_status != expected_status:
        problems.append(f"status={persisted.qualification_status!r} != {expected_status!r}")
    if persisted.qualification_status == QualificationStatus.QUALIFICATION_PENDING.value:
        problems.append("status is still QUALIFICATION_PENDING (outcome not persisted)")
    if persisted.exchange != original.exchange or persisted.primary_exchange != original.primary_exchange:
        problems.append("FIRDS venue fields were modified")
    if expected_status == QualificationStatus.VERIFIED.value:
        if persisted.con_id is None:
            problems.append("VERIFIED without con_id")
        if persisted.qualification_detail not in _VERIFIED_DETAILS:
            problems.append(f"VERIFIED with detail={persisted.qualification_detail!r}")
        venue = (persisted.ibkr_primary_exchange or "").strip().upper()
        if venue in _ROUTING_TOKENS:
            problems.append(f"VERIFIED without a real returned venue ({persisted.ibkr_primary_exchange!r})")
    else:
        if persisted.con_id is not None:
            problems.append(f"non-VERIFIED row carries con_id={persisted.con_id}")
        if persisted.ibkr_primary_exchange is not None:
            problems.append("non-VERIFIED row carries ibkr_primary_exchange")
    return None if not problems else "checkpoint verification failed: " + "; ".join(problems)


async def qualify_one_instrument(store: Any, client: Any, inst: Any, run_id: str, seq: int, guard: dict,
                                 max_attempts: int) -> tuple[str, bool, bool, str, int]:
    """One instrument through the engine's exact per-instrument path → (status, has_conid, aborted,
    reason, seq). A guard tripped BEFORE the request leaves the row untouched; a guard tripped DURING the
    request leaves it QUALIFICATION_PENDING (outcome NOT written)."""
    if guard_tripped(guard):
        return ("", False, True,
                f"tripwire before request: violation={guard['violation']} error_abort={guard['error_abort']} "
                "(row untouched)", seq)
    attempts = store.iq_mark_pending(inst.instrument_id, run_id)
    status, matched, reason, cand_count, conn_lost, count_attempt = await _qualify_one(
        client, inst, attempts, max_attempts)
    if guard_tripped(guard):
        return (status.value, False, True,
                f"tripwire: violation={guard['violation']} error_abort={guard['error_abort']} "
                "(outcome NOT written; row left QUALIFICATION_PENDING)", seq)
    verification, tradability, market_data, con_id, set_lv = _outcome_fields(status, matched)
    verified = status is QualificationStatus.VERIFIED and matched is not None
    ibkr_primary_exchange = (_venue_of_record(matched) or None) if verified else None
    if status is QualificationStatus.VERIFIED and con_id is not None:   # fail-closed conId collision guard
        owner = store.iq_find_instrument_by_conid(con_id)
        if owner is not None and owner.instrument_id != inst.instrument_id:
            status, reason = QualificationStatus.AMBIGUOUS, (
                f"conId {con_id} already assigned to {owner.instrument_id}")
            verification, tradability, market_data, con_id, set_lv = None, None, None, None, False
            ibkr_primary_exchange = None
    detail = _qualification_detail(status, reason, inst, matched)
    seq += 1
    store.iq_apply_outcome(
        inst.instrument_id, run_id=run_id, qualification_status=status.value, reason=reason,
        verification_status=verification, tradability_status=tradability, market_data_status=market_data,
        con_id=con_id, set_last_verified=set_lv, count_attempt=count_attempt,
        qualification_detail=detail, ibkr_primary_exchange=ibkr_primary_exchange,
        event={"id": f"{run_id}-e{seq}", "seq": seq, "market": inst.exchange or "",
               "instrument_id": inst.instrument_id, "event_type": "QUALIFY_RESULT",
               "severity": "ERROR" if "ERROR" in status.value else "INFO",
               "status": status.value, "con_id": con_id, "candidate_count": cand_count,
               "detail": detail, "ibkr_primary_exchange": ibkr_primary_exchange, "reason": reason})
    real_error = ((status.value in _ABORT_STATUSES and count_attempt)
                  or status is QualificationStatus.MARKET_DATA_NOT_ENTITLED)
    return status.value, con_id is not None, bool(conn_lost or real_error), reason, seq


async def run_phase(store: Any, client: Any, rows: list, run_id: str, seq: int, guard: dict, pause: float,
                    label: str) -> tuple[list, bool, int]:
    results: list = []
    for i, inst in enumerate(rows):
        status, has_conid, aborted, reason, seq = await qualify_one_instrument(
            store, client, inst, run_id, seq, guard, MAX_ATTEMPTS)
        results.append({"instrument_id": inst.instrument_id, "asset_class": inst.asset_class,
                        "exchange": inst.exchange, "status": status, "con_id": has_conid,
                        "aborted": aborted, "reason": reason})
        print(f"[{label} {i + 1}/{len(rows)}] {inst.asset_class}@{inst.exchange} → {status or '-'}"
              f"{' con_id' if has_conid else ''} | {reason[:120]}")
        if aborted:
            print(f"[{label}] ABORT at {inst.asset_class}@{inst.exchange}: {reason}")
            return results, True, seq
        if i + 1 < len(rows):
            await _sleep(pause)
    return results, False, seq


async def main(args: argparse.Namespace) -> int:
    store = open_store(resolve_store_url(args.store_url), migrate=False)
    try:
        rows = select_targets(store, source_run_id=args.source_run, expect=args.expect, max_rows=args.max)
    except SelectionMismatch as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"PLAN source_run={args.source_run} selected={len(rows)} (expect {args.expect}), cash first:")
    for r in rows:
        print(f"  {r.instrument_id} {r.asset_class}@{r.exchange} ccy={r.trading_currency} "
              f"status={r.qualification_status} attempts={r.qualification_attempts}")
    if args.dry_run:
        print("DRY-RUN: no connection opened, nothing written.")
        return 0

    run_id = uuid.uuid4().hex
    checksum = qualification_request_checksum(RUN_LABEL, None, False)
    store.iq_create_run(run_id=run_id, request_checksum=checksum, run_label=RUN_LABEL, exchange=None,
                        batch_size=1, pause_seconds=args.pause)
    store.iq_advance_run_status(run_id, "PLANNED", "RUNNING")
    # From here on the run row is RUNNING: EVERYTHING below — planning, SDK construction, handshake, the
    # phases — is inside the try so no failure can leave it RUNNING or the socket open.
    markets: list = []
    seq = 0
    guard = new_guard()
    ib: Any = None
    results: list = []
    aborted, abort_reason = False, None
    try:
        markets = sorted({r.exchange or "" for r in rows})
        store.iq_set_planned_markets(run_id, markets)
        seq = store.iq_max_event_seq(run_id)
        ib = make_ib()
        install_guards(ib, guard)
        client = IbkrQualificationClient(ib, request_timeout=args.request_timeout)
        await connect(ib, args.host, args.port, args.client_id, args.connect_timeout)
        if guard_tripped(guard):
            aborted, abort_reason = True, f"tripwire during handshake: {guard}"
        else:
            res, aborted, seq = await run_phase(store, client, rows[:1], run_id, seq, guard,
                                                args.pause, "CHECKPOINT")
            results += res
            if not aborted:
                cp = store.im_get_instrument(rows[0].instrument_id)
                problem = verify_checkpoint(cp, run_id=run_id, expected_status=results[0]["status"],
                                            original=rows[0])
                print(f"CHECKPOINT read back: status={getattr(cp, 'qualification_status', None)} "
                      f"detail={getattr(cp, 'qualification_detail', None)} "
                      f"venue={getattr(cp, 'ibkr_primary_exchange', None)} "
                      f"con_id={getattr(cp, 'con_id', None)} run={getattr(cp, 'qualification_run_id', None)}"
                      f" → {'OK' if problem is None else 'MISMATCH'}")
                if problem is not None:
                    aborted, abort_reason = True, problem
            if not aborted and len(rows) > 1:
                await _sleep(args.pause)                         # documented pause checkpoint → rest
                res, aborted, seq = await run_phase(store, client, rows[1:], run_id, seq, guard,
                                                    args.pause, "REST")
                results += res
            if aborted and abort_reason is None:
                abort_reason = next((r["reason"] for r in results if r["aborted"]), "aborted")
    except Exception as exc:  # noqa: BLE001 — never leave the run RUNNING or the socket open
        aborted, abort_reason = True, f"{type(exc).__name__}: {exc}"
    finally:
        if ib is not None:
            with contextlib.suppress(Exception):
                ib.disconnect()

    try:
        for m in markets:
            seq += 1
            store.iq_record_market(run_id, market=m, market_status="ABORTED" if aborted else "COMPLETED",
                                   event={"id": f"{run_id}-e{seq}", "seq": seq, "market": m,
                                          "event_type": "MARKET_ABORTED" if aborted else "MARKET_OK",
                                          "severity": "ERROR" if aborted else "INFO",
                                          "reason": abort_reason})
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never block the terminal flip below
        aborted, abort_reason = True, f"{abort_reason or ''} | market bookkeeping failed: {exc}".strip(" |")
    if aborted:
        store.iq_finalize_run(run_id, status="FAILED", failure_code="ABORTED", failure_reason=abort_reason)
    else:
        store.iq_finalize_run(run_id, status="COMPLETED")
    run = store.iq_get_run(run_id)
    print(f"RUN_STATUS={run.status} run_id={run_id} verified={run.verified_count} "
          f"ambiguous={run.ambiguous_count} not_tradable={run.not_tradable_count} mdne={run.mdne_count} "
          f"err_retry={run.error_retryable_count} err_perm={run.error_permanent_count} "
          f"processed={run.processed_count} ABORTED={'yes' if aborted else 'no'}")
    print(f"allowlist: sent_ids={guard['sent_ids']} violation={guard['violation']} "
          f"errors={guard['errors']} error_abort={guard['error_abort']}")
    return 1 if aborted else 0


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bounded IBKR re-qualification of the WP11 backlog (broker read-only; writes outcomes)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="4002 IB Gateway paper")
    p.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    p.add_argument("--store-url", default=None, help="else ATP_DATABASE_URL / atp.env app creds")
    p.add_argument("--source-run", default=SOURCE_RUN_ID, help="prior run whose ERROR_RETRYABLE rows to redo")
    p.add_argument("--expect", type=int, default=DEFAULT_EXPECT, help="exact selection size or refuse")
    p.add_argument("--max", type=int, default=DEFAULT_EXPECT, help="hard cap on rows processed")
    p.add_argument("--pause", type=float, default=PAUSE_S)
    p.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT_S)
    p.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT_S)
    p.add_argument("--dry-run", action="store_true", help="print the selection; no connection, no write")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_args())))
