"""WP3 — controlled, read-only IBKR verification & tradability check of the persistent instrument catalogue.

Qualifies instruments from the WP2 `instruments` table against IBKR **contract details / security
definitions**, strictly read-only. The design is fail-closed at every step:

  * An instrument is `VERIFIED` only when EXACTLY ONE returned contract (by `conId`) is consistent with its
    FULL known identity — conId, exchange / primary exchange, currency, asset class, multiplier and expiry —
    never by symbol alone (currency + venue + asset-class are always required constraints).
  * Several plausible matches ⇒ `AMBIGUOUS` (never a guess).
  * Missing or conflicting contract details ⇒ never `VERIFIED`.
  * A missing IBKR connection or entitlement produces a visible error status.
  * No invented `conId` / exchange / currency / multiplier — only values IBKR actually returned are stored.

Rate-limit-safe (configurable batch size + pause), idempotent (a re-run skips already-terminal instruments),
resumable (a crashed RUNNING run is resumed; per-instrument state lives on the row), with per-instrument AND
per-market error isolation and an immutable audit-event trail.

SAFETY: only `reqContractDetailsAsync` is called on the IBKR client. No orders, no execution, no trading
activation, no market-data subscription purchase, no changes to leverage / kill-switch / risk limits.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .global_catalog import GlobalContract
from .ibkr_catalog import contract_detail_to_global
from .model import canon_decimal_text

if TYPE_CHECKING:  # avoid importing the store at runtime — keeps the import graph free of persistence
    from ..store.base import InstrumentRow


class QualificationStatus(str, Enum):
    """The fail-closed qualification lifecycle. `DISCOVERED` and `ERROR_RETRYABLE` are (re)selectable;
    `QUALIFICATION_PENDING` marks an in-flight/interrupted attempt (re-selected on resume); the rest are
    terminal for a given attempt set."""

    DISCOVERED = "DISCOVERED"
    QUALIFICATION_PENDING = "QUALIFICATION_PENDING"
    VERIFIED = "VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_TRADABLE = "NOT_TRADABLE"
    MARKET_DATA_NOT_ENTITLED = "MARKET_DATA_NOT_ENTITLED"
    ERROR_RETRYABLE = "ERROR_RETRYABLE"
    ERROR_PERMANENT = "ERROR_PERMANENT"


# Selectable for (re)qualification. VERIFIED is added only when the caller explicitly requalifies.
SELECTABLE_STATUSES = (
    QualificationStatus.DISCOVERED.value,
    QualificationStatus.QUALIFICATION_PENDING.value,
    QualificationStatus.ERROR_RETRYABLE.value,
)


# --------------------------------------------------------------------------- typed IBKR outcomes / errors
class QualificationError(Exception):
    """Base for classified, read-only qualification failures. Carries a short machine reason code."""

    def __init__(self, reason: str, *, code: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class RetryableQualificationError(QualificationError):
    """Transient (pacing / connectivity / timeout) — the instrument stays selectable for a later retry."""


class PermanentQualificationError(QualificationError):
    """Non-transient (malformed request / unsupported) — not auto-retried."""


class NotTradableError(QualificationError):
    """IBKR indicated the contract is not tradable for this account/venue."""


class MarketDataNotEntitledError(QualificationError):
    """IBKR indicated no market-data entitlement. WP3 never buys/activates data — it only records the fact."""


class ConnectionUnavailableError(RetryableQualificationError):
    """No usable IBKR connection. Produces a visible ERROR_RETRYABLE status and aborts the run early."""


# Best-effort IBKR error-code taxonomy (used by the adapter to classify surfaced IBKR errors). Conservative:
# unknown codes are treated as retryable so nothing is silently marked permanent.
_MDNE_CODES = frozenset({10089, 10090, 10091, 10167, 10168, 10197, 354})
_NOT_TRADABLE_CODES = frozenset({200})          # "No security definition has been found for the request"
_PERMANENT_CODES = frozenset({321, 322, 478})   # malformed/invalid request
_CONNECTION_CODES = frozenset({502, 504, 1100, 2110})
_RETRYABLE_CODES = frozenset({100, 102, 103, 1101, 1102, 1300, 2103, 2105, 2157, 10147, 10148})


def classify_ibkr_error(code: int | None, message: str = "") -> QualificationError:
    """Map an IBKR error code to a typed, fail-closed qualification error."""
    msg = message or (f"IBKR error {code}" if code is not None else "IBKR error")
    if code in _MDNE_CODES:
        return MarketDataNotEntitledError(msg, code=str(code))
    if code in _NOT_TRADABLE_CODES:
        return NotTradableError(msg, code=str(code))
    if code in _PERMANENT_CODES:
        return PermanentQualificationError(msg, code=str(code))
    if code in _CONNECTION_CODES:
        return ConnectionUnavailableError(msg, code=str(code))
    return RetryableQualificationError(msg, code=str(code) if code is not None else "")


# --------------------------------------------------------------------------- read-only request + matching
_IB_SEC_TYPE = {
    "equity": "STK", "etf": "STK", "index": "IND", "fx": "CASH", "bond": "BOND",
    "future": "FUT", "option": "OPT", "crypto": "CRYPTO", "fund": "FUND",
    "warrant": "WAR", "certificate": "CERT", "cfd": "CFD", "commodity": "CMDTY",
}


def asset_class_to_ib_sec_type(asset_class: str) -> str:
    return _IB_SEC_TYPE.get((asset_class or "").strip().lower(), (asset_class or "").strip().upper())


@dataclass(frozen=True, slots=True)
class QualificationRequest:
    """A read-only IBKR contract-details query built from an instrument's KNOWN identity (never symbol only)."""

    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    con_id: int | None = None
    local_symbol: str = ""


def build_request_spec(instrument: InstrumentRow) -> QualificationRequest:
    return QualificationRequest(
        symbol=instrument.symbol,
        sec_type=asset_class_to_ib_sec_type(instrument.asset_class),
        exchange=instrument.exchange,
        primary_exchange=instrument.primary_exchange or instrument.exchange,
        currency=instrument.trading_currency,
        expiry=instrument.expiry or "",
        strike=(float(instrument.strike) if instrument.strike not in (None, "") else None),
        right=instrument.option_right or "",
        con_id=(int(instrument.con_id) if instrument.con_id is not None else None),
        local_symbol=instrument.local_symbol or "",
    )


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


# Routing / aggregate pseudo-venues that are NOT a real listing venue. IBKR stamps exchange="SMART" on every
# smart-routed STK/ETF contract detail, so "SMART" (and its kin) must never satisfy the venue constraint —
# otherwise an instrument whose stored venue is a routing token would "match" on the routing token alone and
# verify without its actual listing exchange ever being confirmed. Empty is included so a NULL venue is a
# non-venue too. The venue match therefore requires a shared REAL exchange (e.g. NASDAQ / NYSE / CME).
_NON_VENUE_TOKENS = frozenset({"", "SMART", "SMARTUS", "IBKRATS", "OVERNIGHT", "VALUE", "DARKPOOL"})


def _real_venues(*tokens: str) -> set:
    return {v for v in (_norm(t) for t in tokens) if v not in _NON_VENUE_TOKENS}


def _class_compatible(instrument_class: str, candidate_class: str) -> bool:
    """Asset classes must match. The single documented relaxation: IBKR represents ETFs as `STK` and only
    reclassifies to ETF when `stockType` is present, so equity↔etf are treated as compatible. Everything
    else is strict."""
    a, b = (instrument_class or "").lower(), (candidate_class or "").lower()
    if a == b:
        return True
    return {a, b} <= {"equity", "etf"}


def _consistent(instrument: InstrumentRow, c: GlobalContract) -> bool:
    """True iff candidate `c` is consistent with EVERY known identity field of `instrument`. Currency, venue
    and asset class are always required, so a symbol alone can never make a candidate consistent."""
    if c.con_id is None or c.con_id <= 0:
        return False
    if c.asset_class is None or not _class_compatible(instrument.asset_class, c.asset_class.value):
        return False
    inst_ccy = _norm(instrument.trading_currency)
    if not inst_ccy or _norm(c.currency) != inst_ccy:
        return False
    # Venue must match on a shared REAL exchange. Empty strings and routing pseudo-venues (SMART, …) are
    # excluded, so neither a NULL venue nor the ubiquitous "SMART" routing token can satisfy the constraint
    # by coincidence — that would verify an instrument whose actual listing venue was never confirmed.
    inst_venues = _real_venues(instrument.exchange, instrument.primary_exchange)
    cand_venues = _real_venues(c.exchange, c.primary_exchange)
    if not (inst_venues & cand_venues):
        return False
    if instrument.con_id is not None and int(instrument.con_id) != c.con_id:
        return False
    if instrument.multiplier and canon_decimal_text(instrument.multiplier) != canon_decimal_text(c.multiplier):
        return False
    if instrument.expiry and _norm(instrument.expiry) != _norm(c.expiry):
        return False
    if instrument.strike and canon_decimal_text(instrument.strike) != canon_decimal_text(c.strike):
        return False
    return not (instrument.option_right and _norm(instrument.option_right) != _norm(c.right))


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    status: QualificationStatus
    matched: GlobalContract | None
    reason: str
    candidate_count: int
    consistent_con_ids: tuple[int, ...]


def match_contract(instrument: InstrumentRow, candidates: Iterable[GlobalContract]) -> MatchOutcome:
    """Fail-closed identity resolution. Groups returned contracts by `conId` (IBKR returns one row per valid
    exchange for the same contract), keeps only conIds with a venue-consistent row, and requires EXACTLY ONE."""
    cand = list(candidates)
    by_conid: dict[int, list[GlobalContract]] = {}
    for c in cand:
        if c.con_id and c.con_id > 0:
            by_conid.setdefault(c.con_id, []).append(c)
    consistent_ids = tuple(sorted(cid for cid, rows in by_conid.items()
                                  if any(_consistent(instrument, r) for r in rows)))
    if len(consistent_ids) == 1:
        cid = consistent_ids[0]
        matched = next(r for r in by_conid[cid] if _consistent(instrument, r))
        return MatchOutcome(QualificationStatus.VERIFIED, matched,
                            "unique contract match", len(cand), consistent_ids)
    if len(consistent_ids) > 1:
        return MatchOutcome(QualificationStatus.AMBIGUOUS, None,
                            f"{len(consistent_ids)} distinct contracts consistent with known identity",
                            len(cand), consistent_ids)
    if not cand:
        return MatchOutcome(QualificationStatus.NOT_TRADABLE, None,
                            "no contract details returned", 0, ())
    return MatchOutcome(QualificationStatus.NOT_TRADABLE, None,
                        f"{len(cand)} contract(s) returned, none consistent with known identity",
                        len(cand), ())


# --------------------------------------------------------------------------- read-only IBKR client
class QualificationClient:
    """Structural protocol: fetch read-only contract details for a request. Implementations MUST NOT place
    orders, request market-data subscriptions, or mutate any account state. Raise a typed
    `QualificationError` subclass to signal a classified outcome; return a (possibly empty) list of raw
    IBKR ContractDetails otherwise."""

    async def fetch_contract_details(self, request: QualificationRequest) -> list[Any]:  # pragma: no cover
        raise NotImplementedError


class IbkrQualificationClient:
    """Adapter over a connected `ib_insync.IB`-like client. The ONLY IBKR call made is
    `reqContractDetailsAsync` (read-only security-definition lookup). `ib_insync` is imported lazily so
    importing this module pulls in no broker SDK."""

    def __init__(self, ib: Any, *, contract_factory: Callable[[QualificationRequest], Any] | None = None) -> None:
        self._ib = ib
        self._contract_factory = contract_factory or self._build_contract

    async def fetch_contract_details(self, request: QualificationRequest) -> list[Any]:
        if self._ib is None or (hasattr(self._ib, "isConnected") and not self._ib.isConnected()):
            raise ConnectionUnavailableError("IBKR connection unavailable", code="no_connection")
        contract = self._contract_factory(request)
        try:
            details = await self._ib.reqContractDetailsAsync(contract)
        except QualificationError:
            raise
        except Exception as exc:  # surface any client fault as a classified retryable error
            raise RetryableQualificationError(f"contract-details request failed: {exc}") from exc
        return list(details or [])

    @staticmethod
    def _build_contract(request: QualificationRequest) -> Any:
        import ib_insync  # lazy: no broker SDK is imported at module load

        kwargs: dict[str, Any] = {
            "symbol": request.symbol,
            "secType": request.sec_type,
            "currency": request.currency,
        }
        if request.con_id:
            kwargs["conId"] = request.con_id
        if request.sec_type == "STK":
            kwargs["exchange"] = "SMART"
            kwargs["primaryExchange"] = request.primary_exchange or request.exchange
        else:
            kwargs["exchange"] = request.exchange
        if request.expiry:
            kwargs["lastTradeDateOrContractMonth"] = request.expiry
        if request.strike:
            kwargs["strike"] = request.strike
        if request.right:
            kwargs["right"] = request.right
        if request.local_symbol:
            kwargs["localSymbol"] = request.local_symbol
        return ib_insync.Contract(**kwargs)


# --------------------------------------------------------------------------- orchestrator
@dataclass(frozen=True, slots=True)
class QualificationConfig:
    batch_size: int = 25
    pause_seconds: float = 1.0
    max_attempts: int = 3
    limit: int = 500
    exchange: str | None = None
    requalify_verified: bool = False


@dataclass(slots=True)
class QualificationSummary:
    run_id: str
    status: str
    processed: int = 0
    verified: int = 0
    ambiguous: int = 0
    not_tradable: int = 0
    market_data_not_entitled: int = 0
    error_retryable: int = 0
    error_permanent: int = 0
    completed_markets: list = field(default_factory=list)
    failed_markets: list = field(default_factory=list)
    resumed: bool = False
    connection_lost: bool = False


def qualification_request_checksum(run_label: str, exchange: str | None, requalify: bool) -> str:
    payload = {"label": run_label, "exchange": exchange or "*", "requalify": bool(requalify),
               "tag": "atp.instrument-qualification.request.v1"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _outcome_fields(status: QualificationStatus, matched: GlobalContract | None):
    """Map a qualification status to the coarse WP2 fields. Fail-closed: only a unique VERIFIED asserts an
    identity; nothing here asserts tradability or a data entitlement that was not proven. Returns
    (verification_status, tradability_status, market_data_status, con_id, set_last_verified)."""
    if status is QualificationStatus.VERIFIED:
        return "verified", None, None, (matched.con_id if matched else None), True
    if status is QualificationStatus.NOT_TRADABLE:
        return None, "not_tradable", None, None, False
    if status is QualificationStatus.MARKET_DATA_NOT_ENTITLED:
        return None, None, "none", None, False
    # AMBIGUOUS / ERROR_* leave the coarse fields unchanged (never fabricate a verification)
    return None, None, None, None, False


async def qualify_instruments(store, client: QualificationClient, *, run_label: str,
                              config: QualificationConfig | None = None, run_id: str | None = None,
                              sleep: Callable[[float], Awaitable[None]] | None = None) -> QualificationSummary:
    """Run (or resume) a read-only IBKR qualification pass over the persistent instrument catalogue."""
    config = config or QualificationConfig()
    do_sleep = sleep or asyncio.sleep
    checksum = qualification_request_checksum(run_label, config.exchange, config.requalify_verified)

    # Each invocation is its OWN run (fresh run_id). We deliberately never re-enter a still-RUNNING run row:
    # a RUNNING row cannot be told apart from a live worker's without a lease, so re-entering one would let two
    # overlapping workers collide (duplicate event ids, double-counted attempts). Resumability is achieved by
    # SELECTION instead — a crashed pass leaves its instruments in QUALIFICATION_PENDING / ERROR_RETRYABLE, and
    # those are re-selected here; an orphaned RUNNING run is swept separately by iq_reclaim_stale_running.
    run_id = run_id or uuid.uuid4().hex
    store.iq_create_run(run_id=run_id, request_checksum=checksum, run_label=run_label,
                        exchange=config.exchange, batch_size=config.batch_size,
                        pause_seconds=config.pause_seconds)
    store.iq_advance_run_status(run_id, "PLANNED", "RUNNING")

    statuses = list(SELECTABLE_STATUSES)
    if config.requalify_verified:
        statuses.append(QualificationStatus.VERIFIED.value)
    instruments = store.iq_select_instruments(statuses=statuses, exchange=config.exchange, limit=config.limit)
    # 'resumed' = this pass is picking up work a prior (crashed/failed) pass left incomplete.
    resumed = any(i.qualification_status in ("QUALIFICATION_PENDING", "ERROR_RETRYABLE") for i in instruments)

    # Group by market (exchange) for per-market isolation; deterministic ordering.
    markets: dict[str, list] = {}
    for inst in instruments:
        markets.setdefault(inst.exchange or "", []).append(inst)
    store.iq_set_planned_markets(run_id, sorted(markets))
    seq = store.iq_max_event_seq(run_id)   # resume-safe: continue after the true max, never re-use an id
    connection_lost = False

    for market in sorted(markets):
        insts = sorted(markets[market], key=lambda i: i.instrument_id)
        try:
            for start in range(0, len(insts), max(1, config.batch_size)):
                batch = insts[start:start + max(1, config.batch_size)]
                for inst in batch:
                    attempts = store.iq_mark_pending(inst.instrument_id, run_id)
                    status, matched, reason, cand_count, conn_lost = await _qualify_one(
                        client, inst, attempts, config.max_attempts)
                    verification, tradability, market_data, con_id, set_lv = _outcome_fields(status, matched)
                    # Fail-closed conId collision guard: never overwrite another instrument's verified conId.
                    if status is QualificationStatus.VERIFIED and con_id is not None:
                        owner = store.iq_find_instrument_by_conid(con_id)
                        if owner is not None and owner.instrument_id != inst.instrument_id:
                            status, reason = QualificationStatus.AMBIGUOUS, (
                                f"conId {con_id} already assigned to {owner.instrument_id}")
                            verification, tradability, market_data, con_id, set_lv = None, None, None, None, False
                    seq += 1
                    store.iq_apply_outcome(
                        inst.instrument_id, run_id=run_id, qualification_status=status.value, reason=reason,
                        verification_status=verification, tradability_status=tradability,
                        market_data_status=market_data, con_id=con_id, set_last_verified=set_lv,
                        count_attempt=not conn_lost,   # a broker outage must not consume the retry budget
                        event={"id": f"{run_id}-e{seq}", "seq": seq, "market": market,
                               "instrument_id": inst.instrument_id, "event_type": "QUALIFY_RESULT",
                               "severity": "ERROR" if "ERROR" in status.value else "INFO",
                               "status": status.value, "con_id": con_id, "candidate_count": cand_count,
                               "reason": reason})
                    if conn_lost:                          # ConnectionUnavailableError by type — abort the run
                        connection_lost = True
                        break
                if connection_lost:
                    break
                await do_sleep(config.pause_seconds)   # rate-limit between batches
            if connection_lost:
                seq += 1
                store.iq_record_market(run_id, market=market, market_status="ABORTED",
                                       event={"id": f"{run_id}-e{seq}", "seq": seq, "market": market,
                                              "event_type": "MARKET_ABORTED", "severity": "ERROR",
                                              "reason": "IBKR connection unavailable"})
                break
            seq += 1
            store.iq_record_market(run_id, market=market, market_status="COMPLETED",
                                   event={"id": f"{run_id}-e{seq}", "seq": seq, "market": market,
                                          "event_type": "MARKET_OK", "severity": "INFO"})
        except Exception as exc:  # noqa: BLE001 — per-market isolation: never let one market abort the rest
            seq += 1
            store.iq_record_market(run_id, market=market, market_status="FAILED",
                                   event={"id": f"{run_id}-e{seq}", "seq": seq, "market": market,
                                          "event_type": "MARKET_ERROR", "severity": "ERROR",
                                          "reason": f"{type(exc).__name__}: {exc}"})
        if connection_lost:
            break

    run = store.iq_get_run(run_id)
    completed_markets = json.loads(run.completed_markets_json)
    failed_markets = json.loads(run.failed_markets_json)
    if connection_lost:
        final, failure = "FAILED", ("CONNECTION_UNAVAILABLE", "IBKR connection unavailable")
    elif failed_markets and completed_markets:
        final, failure = "PARTIAL", ("PARTIAL_QUALIFICATION", f"{len(failed_markets)} market(s) failed")
    elif failed_markets:
        final, failure = "FAILED", ("ALL_MARKETS_FAILED", "every market failed")
    else:
        final, failure = "COMPLETED", (None, None)
    store.iq_finalize_run(run_id, status=final, failure_code=failure[0], failure_reason=failure[1])
    return _summary(store.iq_get_run(run_id), resumed=resumed, connection_lost=connection_lost)


async def _qualify_one(client: QualificationClient, inst, attempts: int, max_attempts: int):
    """Qualify a single instrument with full per-instrument error isolation → (status, matched, reason,
    candidate_count, connection_lost). Never raises. `connection_lost` is set ONLY for a
    ConnectionUnavailableError (by exception TYPE, never by message text), so the run-abort decision cannot
    be spoofed or missed by an instrument's error message. `attempts` is the count of prior RECORDED
    outcomes, so this attempt is number `attempts + 1` for the escalation check."""
    this_attempt = attempts + 1
    try:
        request = build_request_spec(inst)
        details = await client.fetch_contract_details(request)
        candidates = [contract_detail_to_global(d) for d in details]
        outcome = match_contract(inst, candidates)
        return outcome.status, outcome.matched, outcome.reason, outcome.candidate_count, False
    except MarketDataNotEntitledError as exc:
        return QualificationStatus.MARKET_DATA_NOT_ENTITLED, None, exc.reason, 0, False
    except NotTradableError as exc:
        return QualificationStatus.NOT_TRADABLE, None, exc.reason, 0, False
    except PermanentQualificationError as exc:
        return QualificationStatus.ERROR_PERMANENT, None, exc.reason, 0, False
    except ConnectionUnavailableError as exc:
        return QualificationStatus.ERROR_RETRYABLE, None, exc.reason, 0, True
    except RetryableQualificationError as exc:
        status = (QualificationStatus.ERROR_PERMANENT if this_attempt >= max_attempts
                  else QualificationStatus.ERROR_RETRYABLE)
        return status, None, exc.reason, 0, False
    except Exception as exc:  # noqa: BLE001 — unknown fault: conservative retry, escalate when exhausted
        status = (QualificationStatus.ERROR_PERMANENT if this_attempt >= max_attempts
                  else QualificationStatus.ERROR_RETRYABLE)
        return status, None, f"{type(exc).__name__}: {exc}", 0, False


def _summary(run, *, resumed: bool, connection_lost: bool) -> QualificationSummary:
    return QualificationSummary(
        run_id=run.run_id, status=run.status, processed=run.processed_count, verified=run.verified_count,
        ambiguous=run.ambiguous_count, not_tradable=run.not_tradable_count,
        market_data_not_entitled=run.mdne_count, error_retryable=run.error_retryable_count,
        error_permanent=run.error_permanent_count,
        completed_markets=json.loads(run.completed_markets_json),
        failed_markets=json.loads(run.failed_markets_json), resumed=resumed, connection_lost=connection_lost,
    )
