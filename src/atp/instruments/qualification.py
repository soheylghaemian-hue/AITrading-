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
import contextlib
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .global_catalog import GlobalContract
from .ibkr_catalog import contract_detail_to_global
from .ibkr_venue import is_ibkr_exchange, resolve_ibkr_exchanges
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


class AmbiguousContractError(QualificationError):
    """§ WP11 — IBKR reported the contract description is AMBIGUOUS (error 200 "...is ambiguous"): the query
    matched several contracts but IBKR returned none because it was under-specified. This is a genuine
    ambiguity (→ AMBIGUOUS), NOT a venue-resolution gap and NOT a not-found; it needs a more specific query
    (more identity), never a blind identical re-query — so it is terminal for this attempt set, not
    ERROR_RETRYABLE (which would re-issue the same ambiguous query forever)."""


class VenueResolutionError(RetryableQualificationError):
    """§ WP10 — the request could not be resolved to a real IBKR venue/contract because it used an
    unmapped/invalid venue (a FIRDS MIC is NOT an IBKR exchange code), lacked a usable identifier, or IBKR
    rejected the destination / contract spec (error 200 "destination or exchange selected is Invalid" /
    "Invalid value in field # NNN"). This is a QUERY / venue-resolution failure — explicitly NOT a verdict
    on tradability. It maps to a re-selectable ERROR_RETRYABLE that is BUDGET-NEUTRAL (never consumes the
    retry budget and so never auto-escalates to ERROR_PERMANENT), so an instrument is never permanently
    marked failed while the query itself is at fault. A future migration could give it a dedicated
    VENUE_UNRESOLVED status; until then the reason text carries the distinction."""


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


# § WP10 — IBKR reuses error code 200 for TWO very different situations: a genuine "No security definition
# has been found for the request" (a real not-found for a WELL-FORMED query) AND venue/contract-spec
# rejections such as "The destination or exchange selected is Invalid" or "Invalid value in field # NNN"
# (field 541 was seen for FUT/OPT). Only the former is evidence about tradability; the latter is a
# query/venue-resolution problem. These markers separate them by message text (lower-cased).
_VENUE_ERROR_MARKERS = (
    "destination or exchange selected is invalid",
    "invalid destination",
    "invalid value in field",
    "the exchange is closed",
    "no trading permissions",  # entitlement to the venue, not a tradability verdict
)
_NO_SECURITY_DEF_MARKERS = ("no security definition",)
# § WP11 — IBKR's OTHER documented error-200 message: "The contract description specified for <X> is
# ambiguous" (resolved by a more specific query, not a retry). Distinct from a venue gap and a not-found.
_AMBIGUOUS_MARKERS = ("is ambiguous", "ambiguous")


def classify_contract_query_error(captured: Iterable[tuple[int | None, str]]) -> QualificationError | None:
    """§ WP10 — given the IBKR error events captured during a contract-details request that returned NO
    contracts, decide whether the emptiness is a venue/query-resolution failure (→ VenueResolutionError) or
    a genuine "no security definition" (→ None, so the caller records the ordinary NOT_TRADABLE outcome).
    Fail-open toward re-query: an error-200 we cannot positively attribute to "no security definition" is
    treated as venue-resolution, never as proof of non-tradability. Non-200 codes defer to
    classify_ibkr_error but only when they carry a meaningful (non-informational) classification."""
    for code, message in captured:
        msg = (message or "").lower()
        if code == 200:
            if any(m in msg for m in _VENUE_ERROR_MARKERS):
                return VenueResolutionError(f"venue/query resolution failed (IBKR 200): {message}",
                                            code="venue_unresolved")
            if any(m in msg for m in _AMBIGUOUS_MARKERS):   # § WP11 — genuine ambiguity, not a venue gap
                return AmbiguousContractError(f"IBKR reported an ambiguous contract (200): {message}",
                                              code="ambiguous")
            if any(m in msg for m in _NO_SECURITY_DEF_MARKERS):
                continue   # genuine not-found → fall through to NOT_TRADABLE
            # an error 200 we cannot attribute to a genuine not-found: do NOT assert non-tradability
            return VenueResolutionError(f"unattributed IBKR error 200: {message}", code="venue_unresolved")
        if code is not None:
            err = classify_ibkr_error(code, message)
            if isinstance(err, (NotTradableError, MarketDataNotEntitledError, PermanentQualificationError,
                                ConnectionUnavailableError)):
                return err
    return None  # genuine "no security definition" (or nothing captured) → empty result → NOT_TRADABLE


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
    exchange: str            # § WP10: this is the FIRDS ISO-10383 MIC, NOT an IBKR exchange code
    primary_exchange: str    # § WP10: also a FIRDS MIC (FIRDS copies the venue MIC here)
    currency: str
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    con_id: int | None = None
    local_symbol: str = ""
    isin: str = ""           # § WP10: drives ISIN-based discovery (secIdType='ISIN', secId=<isin>)


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
        isin=getattr(instrument, "isin", "") or "",
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


def _venue_resolvable(instrument: InstrumentRow) -> bool:
    """§ WP10 — can the instrument's stored venue be translated to an IBKR exchange code (or is it already
    one)? If not, we can neither VERIFY the venue nor declare NOT_TRADABLE on a venue basis: a returned
    contract that is inconsistent only because the venue is unconfirmable is a venue-resolution gap, not a
    tradability verdict (handled in _qualify_one)."""
    e, p = getattr(instrument, "exchange", ""), getattr(instrument, "primary_exchange", "")
    return bool(resolve_ibkr_exchanges(e) or resolve_ibkr_exchanges(p)
                or is_ibkr_exchange(e) or is_ibkr_exchange(p))


_DERIVATIVE_CLASSES = frozenset({"future", "option"})


def _instrument_is_derivative(instrument: InstrumentRow) -> bool:
    return (getattr(instrument, "asset_class", "") or "").strip().lower() in _DERIVATIVE_CLASSES


def _venue_of_record(c: GlobalContract) -> str:
    """§ WP11 — the REAL (non-routing) IBKR venue to store for a verified contract: its returned
    ``primaryExchange`` (preferred) else ``exchange``. Never SMART/routing, never the FIRDS MIC. '' if the
    reply carried no real venue (then a cash contract cannot be VERIFIED — see _isin_echo_match)."""
    for token in (getattr(c, "primary_exchange", ""), getattr(c, "exchange", "")):
        n = _norm(token)
        if n and n not in _NON_VENUE_TOKENS:
            return n
    return ""


def _venue_match(instrument: InstrumentRow, c: GlobalContract) -> bool:
    """§ WP10 anchor (B) — the returned REAL venue intersects the registry translation of the instrument's
    FIRDS MIC. The instrument's stored venue is a FIRDS MIC; the returned venue is an IBKR code (XPAR vs
    SBF), so we translate via the fail-closed registry before intersecting. An unmapped MIC yields NO
    expected venue → False (the venue is unconfirmable). Routing pseudo-venues (SMART, …) and NULLs are
    excluded from the returned side, so neither can satisfy the constraint by coincidence."""
    expected_venues = {v.upper() for v in
                       (*resolve_ibkr_exchanges(instrument.exchange),
                        *resolve_ibkr_exchanges(instrument.primary_exchange)) if v}
    for raw in (instrument.exchange, instrument.primary_exchange):   # US sources store IBKR codes, not MICs
        if is_ibkr_exchange(raw):
            expected_venues.add(_norm(raw))
    cand_venues = _real_venues(c.exchange, c.primary_exchange)   # IBKR codes, SMART/routing excluded
    return bool(expected_venues and (expected_venues & cand_venues))


def _isin_echo_match(instrument: InstrumentRow, c: GlobalContract) -> bool:
    """§ WP11 anchor (A) — IBKR echoed the instrument's EXACT ISIN in secIdList AND returned a real
    (non-routing) venue. Fail-closed: the echo is a POSITIVE anchor only when present and equal; its absence
    yields False here (the caller falls back to anchor B). A real returned venue is required so a verified
    cash line always has a venue of record (§ WP11 area 3)."""
    echo = _norm(getattr(c, "isin", ""))
    want = _norm(getattr(instrument, "isin", ""))
    if not echo or not want or echo != want:
        return False
    return bool(_venue_of_record(c))


def _identity_anchor_ok(instrument: InstrumentRow, c: GlobalContract) -> bool:
    """§ WP11 — a candidate is only an identity match if it satisfies a POSITIVE anchor beyond the ISIN
    search key: (A) an ISIN echo (cash only), or (B) a registry venue-match. Verifying on the search key
    alone (echo absent AND MIC unmapped) is NOT fail-closed and is forbidden. An ISIN echo that is PRESENT
    but DIFFERENT is a hard identity conflict — never consistent, and never rescued by a venue match."""
    echo = _norm(getattr(c, "isin", ""))
    want = _norm(getattr(instrument, "isin", ""))
    if echo and want and echo != want:
        return False
    # Anchor A (ISIN echo) is a CASH anchor; derivatives keep the WP10 venue-match anchor exclusively — their
    # identity is venue+expiry+strike+right+multiplier, and an unmapped-venue derivative never reaches here.
    if not _instrument_is_derivative(instrument) and _isin_echo_match(instrument, c):
        return True
    return _venue_match(instrument, c)


def _consistent(instrument: InstrumentRow, c: GlobalContract) -> bool:
    """True iff candidate `c` is consistent with EVERY known identity field of `instrument`. Currency, asset
    class and a POSITIVE identity anchor (ISIN echo or venue match — see _identity_anchor_ok) are always
    required, so neither a symbol nor the ISIN search key alone can ever make a candidate consistent."""
    if c.con_id is None or c.con_id <= 0:
        return False
    if c.asset_class is None or not _class_compatible(instrument.asset_class, c.asset_class.value):
        return False
    inst_ccy = _norm(instrument.trading_currency)
    if not inst_ccy or _norm(c.currency) != inst_ccy:
        return False
    if not _identity_anchor_ok(instrument, c):
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


def _any_candidate_in_currency(instrument: InstrumentRow, candidates: Iterable[GlobalContract]) -> bool:
    """§ WP11 — does ANY returned candidate carry the requested trading currency (and a real conId)? Used to
    tell a currency deviation (ISIN found, wrong currency → re-queryable) apart from a genuine identity
    mismatch on a known venue."""
    ccy = _norm(instrument.trading_currency)
    return bool(ccy) and any(_norm(getattr(c, "currency", "")) == ccy and (getattr(c, "con_id", 0) or 0) > 0
                             for c in candidates)


# § WP11 — machine-readable sub-classification stored in instruments.qualification_detail (a closed
# vocabulary; NULL when none applies). Derived from the final (status, reason, matched) so it never needs a
# new qualification_status value. `matched` lets a VERIFIED outcome record WHICH anchor confirmed it.
_QUALIFICATION_DETAIL_KINDS = frozenset({
    "verified_isin_echo", "verified_venue_match", "ambiguous", "currency_conflict", "bond_not_found",
    "venue_unresolved", "not_found",
})


def _qualification_detail(status: QualificationStatus, reason: str, instrument: InstrumentRow,
                          matched: GlobalContract | None) -> str | None:
    """Canonical sub-classification for the outcome, or None. Never introduces a new status — it is a
    queryable refinement of the eight existing ones."""
    if status is QualificationStatus.VERIFIED:
        echo = _norm(getattr(matched, "isin", "")) if matched is not None else ""
        want = _norm(getattr(instrument, "isin", ""))
        return "verified_isin_echo" if (echo and want and echo == want) else "verified_venue_match"
    if status is QualificationStatus.AMBIGUOUS:
        return "ambiguous"
    if status is QualificationStatus.NOT_TRADABLE:
        return "not_found"
    if status is QualificationStatus.ERROR_RETRYABLE:
        r = (reason or "").lower()
        for kind in ("currency_conflict", "bond_not_found", "venue_unresolved"):
            if kind in r:
                return kind
    return None


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
    """Adapter over a connected `ib_async.IB`-like client. The ONLY IBKR call made is
    `reqContractDetailsAsync` (read-only security-definition lookup) — never an order, market-data
    subscription, scanner or any account-mutating call. `ib_async` is imported lazily so importing this
    module pulls in no broker SDK (the import-graph guard enforces this)."""

    def __init__(self, ib: Any, *, contract_factory: Callable[[QualificationRequest], Any] | None = None,
                 request_timeout: float | None = 15.0) -> None:
        self._ib = ib
        self._contract_factory = contract_factory or self._build_contract
        self._request_timeout = request_timeout   # hard per-request timeout (s); None disables it

    async def fetch_contract_details(self, request: QualificationRequest) -> list[Any]:
        if self._ib is None or (hasattr(self._ib, "isConnected") and not self._ib.isConnected()):
            raise ConnectionUnavailableError("IBKR connection unavailable", code="no_connection")
        contract = self._contract_factory(request)   # may raise VenueResolutionError (unmapped MIC / no id)

        # § WP10 — capture IBKR error events for this (sequential, one-at-a-time) request so an EMPTY result
        # can be classified by its cause instead of being blindly treated as NOT_TRADABLE.
        captured: list[tuple[int | None, str]] = []
        err_event = getattr(self._ib, "errorEvent", None)

        def _on_error(*args: Any) -> None:
            code = args[1] if len(args) > 1 else None
            message = str(args[2]) if len(args) > 2 else ""
            try:
                captured.append((int(code) if code is not None else None, message))
            except (TypeError, ValueError):
                captured.append((None, message))

        if err_event is not None:
            try:
                err_event += _on_error
            except Exception:  # noqa: BLE001 — an ib without a real Event just stays uninstrumented
                err_event = None
        try:
            call = self._ib.reqContractDetailsAsync(contract)
            if self._request_timeout and self._request_timeout > 0:
                details = await asyncio.wait_for(call, self._request_timeout)
            else:
                details = await call
        except QualificationError:
            raise
        except TimeoutError as exc:                 # hard per-request time budget hit → retryable
            raise RetryableQualificationError(
                f"contract-details request timed out after {self._request_timeout}s", code="timeout") from exc
        except ConnectionError as exc:                      # socket drop (refused/reset/aborted/broken pipe)
            raise ConnectionUnavailableError(f"IBKR connection lost during request: {exc}",
                                             code="connection_lost") from exc
        except Exception as exc:
            # a fault that dropped the connection mid-request is a connection loss (orchestrator aborts the
            # run); anything else is a per-instrument retryable fault (isolated by the orchestrator).
            if hasattr(self._ib, "isConnected") and not self._ib.isConnected():
                raise ConnectionUnavailableError(f"IBKR connection lost during request: {exc}",
                                                 code="connection_lost") from exc
            raise RetryableQualificationError(f"contract-details request failed: {exc}") from exc
        finally:
            if err_event is not None:
                with contextlib.suppress(Exception):
                    err_event -= _on_error

        details = list(details or [])
        if not details:
            # An empty result is NOT automatically NOT_TRADABLE. A venue/query-resolution error 200 →
            # VenueResolutionError (re-queryable); a genuine "no security definition" (or no captured error)
            # → return empty so the matcher records the ordinary NOT_TRADABLE outcome.
            err = classify_contract_query_error(captured)
            if err is not None:
                raise err
        return details

    @staticmethod
    def _build_contract(request: QualificationRequest) -> Any:
        """§ WP10/WP11 — build a read-only contract-details query WITHOUT the namespace bugs that produced the
        canary's spurious NOT_TRADABLE results, with the asset-class-correct discovery path:
          * The FIRDS symbol is the ISIN, not an IBKR ticker — so we NEVER put it in Contract.symbol (except
            for BONDs, see below) and instead discover by ISIN (secIdType='ISIN', secId=<isin>), or by a
            previously-resolved conId.
          * The FIRDS MIC is NOT an IBKR exchange code — so we NEVER send it raw. Cash uses exchange='SMART'
            (search/routing only) plus, when the MIC is in the fail-closed venue registry, an IBKR
            primaryExchange to disambiguate. Derivatives need a concrete IBKR exchange, resolved via the
            registry; an unmapped MIC raises VenueResolutionError (re-queryable) rather than sending a bad
            destination and being misread as NOT_TRADABLE.
          * § WP11 BOND: IBKR resolves bonds by the CUSIP/ISIN placed in Contract.symbol with secType='BOND'
            — NOT via secIdType/secId. WP10's bond query used secIdType='ISIN', which is malformed for BOND,
            so genuine bond lookups returned empty and were misread as NOT_TRADABLE. Route via SMART.
          * § WP11 CASH currency: currency is OMITTED from the ISIN-discovery query so a listing that exists
            only in another currency is OBSERVED (→ non-terminal currency_conflict) rather than collapsing to
            an empty result / false NOT_TRADABLE; currency is re-applied as a Python-side consistency
            constraint (see _consistent). conId/ticker/derivative queries keep currency as an identity field.
        """
        sec_type = request.sec_type
        is_derivative = sec_type in ("FUT", "OPT", "FOP")
        venue = request.exchange or request.primary_exchange

        # --- BOND: ISIN/CUSIP in the symbol field, routed via SMART (never secIdType, never the raw MIC) ---
        if sec_type == "BOND":
            if not request.isin:
                raise VenueResolutionError(
                    "no ISIN for a BOND security-definition query (IBKR resolves bonds by the ISIN/CUSIP in "
                    "the symbol field) — re-query once an identifier is available", code="venue_unresolved")
            import ib_async  # lazy: no broker SDK at module load, and only after the query is validated
            return ib_async.Contract(secType="BOND", symbol=request.isin, exchange="SMART",
                                     currency=request.currency)

        kwargs: dict[str, Any] = {"secType": sec_type}

        # --- venue: translate a FIRDS MIC to its IBKR code; accept a token that is ALREADY an IBKR code
        # (US listing sources); never send a raw ISO MIC ---
        mapped = resolve_ibkr_exchanges(venue)
        if is_derivative:
            ibkr_ex = mapped or ((venue,) if is_ibkr_exchange(venue) else ())
            if not ibkr_ex:
                raise VenueResolutionError(
                    f"no IBKR exchange for venue {venue!r} ({sec_type}); cannot build a valid derivative "
                    "query — re-query once the venue registry maps this MIC", code="venue_unresolved")
            kwargs["exchange"] = ibkr_ex[0]
        else:
            kwargs["exchange"] = "SMART"                      # search/routing only, never a venue assertion
            prim = mapped[0] if mapped else (venue if is_ibkr_exchange(venue) else "")
            if prim:
                kwargs["primaryExchange"] = prim             # disambiguate SMART when the IBKR venue is known

        # --- identity: conId (most precise) > ISIN discovery > a real ticker on an already-IBKR venue.
        # A FIRDS row's symbol IS the ISIN on an ISO MIC (is_ibkr_exchange False), so it never takes the
        # ticker branch — we never send symbol==ISIN. ---
        if request.con_id:
            kwargs["conId"] = request.con_id
            kwargs["currency"] = request.currency
        elif request.isin:
            kwargs["secIdType"] = "ISIN"
            kwargs["secId"] = request.isin
            if is_derivative:
                kwargs["currency"] = request.currency        # derivatives need currency as an identity field
            # § WP11 cash: currency intentionally omitted here (observe currency deviation in Python).
        elif request.symbol and not is_derivative and is_ibkr_exchange(venue):
            kwargs["symbol"] = request.symbol
            kwargs["currency"] = request.currency
        else:
            raise VenueResolutionError(
                "no ISIN, conId or IBKR-venue ticker available — cannot build a reliable IBKR query (a FIRDS "
                "symbol is the ISIN, not an IBKR ticker); re-query once an identifier is available",
                code="venue_unresolved")

        # --- derivative identity (always required for a derivative) ---
        if request.expiry:
            kwargs["lastTradeDateOrContractMonth"] = request.expiry
        if request.strike:
            kwargs["strike"] = request.strike
        if request.right:
            kwargs["right"] = request.right
        import ib_async  # lazy: no broker SDK at module load, and only after the query is validated
        return ib_async.Contract(**kwargs)


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
                    status, matched, reason, cand_count, conn_lost, count_attempt = await _qualify_one(
                        client, inst, attempts, config.max_attempts)
                    verification, tradability, market_data, con_id, set_lv = _outcome_fields(status, matched)
                    # § WP11 — the REAL IBKR venue returned for a verified contract (never the FIRDS MIC,
                    # never SMART). Recorded in a NEW column so the instrument's FIRDS-MIC provenance (used by
                    # resolve_ibkr_exchanges next run) is preserved — overwriting it would break idempotency.
                    _verified = status is QualificationStatus.VERIFIED and matched is not None
                    ibkr_primary_exchange = (_venue_of_record(matched) or None) if _verified else None
                    # Fail-closed conId collision guard: never overwrite another instrument's verified conId.
                    if status is QualificationStatus.VERIFIED and con_id is not None:
                        owner = store.iq_find_instrument_by_conid(con_id)
                        if owner is not None and owner.instrument_id != inst.instrument_id:
                            status, reason = QualificationStatus.AMBIGUOUS, (
                                f"conId {con_id} already assigned to {owner.instrument_id}")
                            verification, tradability, market_data, con_id, set_lv = None, None, None, None, False
                            ibkr_primary_exchange = None
                    detail = _qualification_detail(status, reason, inst, matched)   # § WP11 sub-class
                    seq += 1
                    store.iq_apply_outcome(
                        inst.instrument_id, run_id=run_id, qualification_status=status.value, reason=reason,
                        verification_status=verification, tradability_status=tradability,
                        market_data_status=market_data, con_id=con_id, set_last_verified=set_lv,
                        count_attempt=count_attempt,   # False for broker-outage AND venue-resolution faults
                        qualification_detail=detail, ibkr_primary_exchange=ibkr_primary_exchange,
                        event={"id": f"{run_id}-e{seq}", "seq": seq, "market": market,
                               "instrument_id": inst.instrument_id, "event_type": "QUALIFY_RESULT",
                               "severity": "ERROR" if "ERROR" in status.value else "INFO",
                               "status": status.value, "con_id": con_id, "candidate_count": cand_count,
                               "detail": detail, "ibkr_primary_exchange": ibkr_primary_exchange,
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
    candidate_count, connection_lost, count_attempt). Never raises.

    `connection_lost` is set ONLY for a ConnectionUnavailableError (by exception TYPE, never by message
    text), so the run-abort decision cannot be spoofed or missed by an instrument's error message.
    `count_attempt` (§ WP10) is decoupled from `connection_lost`: it is False for BOTH a broker outage AND a
    venue/query-resolution failure, so neither consumes the instrument's retry budget nor auto-escalates it
    to ERROR_PERMANENT — a venue-resolution problem is the query's fault, not the instrument's. A
    VenueResolutionError is a per-instrument fault (connection_lost stays False, the run is NOT aborted).
    `attempts` is the count of prior RECORDED outcomes, so this attempt is number `attempts + 1`."""
    this_attempt = attempts + 1
    try:
        request = build_request_spec(inst)
        details = await client.fetch_contract_details(request)
        candidates = [contract_detail_to_global(d) for d in details]
        outcome = match_contract(inst, candidates)
        # § WP10/WP11 — a terminal NOT_TRADABLE from the matcher must be RE-EXAMINED before it stands, so a
        # query/venue/currency/universe artifact never becomes a false global tradability verdict.
        if outcome.status is QualificationStatus.NOT_TRADABLE:
            asset = (inst.asset_class or "").strip().lower()
            if outcome.candidate_count == 0:
                # A genuinely EMPTY, well-formed result. For a BOND (§ WP11) this is NOT a global verdict —
                # IBKR's bond universe is entitlement/account-scoped — so it is a re-queryable, budget-neutral
                # gap, never terminal NOT_TRADABLE. For cash, an empty ISIN lookup is a real not-found.
                if asset == "bond":
                    return (QualificationStatus.ERROR_RETRYABLE, None,
                            "bond_not_found: a well-formed BOND ISIN lookup returned nothing — IBKR's bond "
                            "universe is entitlement/account-scoped, so this is not a tradability verdict; "
                            "re-query with the proper entitlement", 0, False, False)
                return outcome.status, outcome.matched, outcome.reason, outcome.candidate_count, False, True
            # Contract(s) WERE returned but none is consistent — determine WHY (fail-closed; a merely
            # unconfirmable identity/venue/currency is re-queryable, NEVER a false NOT_TRADABLE).
            if not _any_candidate_in_currency(inst, candidates):
                # § WP11 (c) — the ISIN resolved to contract(s), but none in the requested currency.
                return (QualificationStatus.ERROR_RETRYABLE, None,
                        f"currency_conflict: {outcome.candidate_count} contract(s) for the ISIN but none in "
                        f"the requested currency {inst.trading_currency!r}; re-query once the currency is "
                        "reconciled", outcome.candidate_count, False, False)
            if not _venue_resolvable(inst):
                # § WP10 — returned-but-inconsistent on an UNMAPPED MIC: a venue-resolution gap, re-queryable.
                return (QualificationStatus.ERROR_RETRYABLE, None,
                        f"venue_unresolved: {outcome.candidate_count} contract(s) returned but MIC "
                        f"{inst.exchange!r} is unmapped — cannot confirm venue; re-query once the registry "
                        "maps it", outcome.candidate_count, False, False)
            # Mapped MIC, currency present, candidates returned but none consistent → a genuine identity
            # mismatch on a KNOWN venue (e.g. wrong venue returned). Keep the terminal NOT_TRADABLE (WP10).
            return outcome.status, outcome.matched, outcome.reason, outcome.candidate_count, False, True
        return outcome.status, outcome.matched, outcome.reason, outcome.candidate_count, False, True
    except MarketDataNotEntitledError as exc:
        return QualificationStatus.MARKET_DATA_NOT_ENTITLED, None, exc.reason, 0, False, True
    except NotTradableError as exc:
        return QualificationStatus.NOT_TRADABLE, None, exc.reason, 0, False, True
    except AmbiguousContractError as exc:          # § WP11: IBKR reported ambiguity → AMBIGUOUS (terminal)
        return QualificationStatus.AMBIGUOUS, None, exc.reason, 0, False, True
    except PermanentQualificationError as exc:
        return QualificationStatus.ERROR_PERMANENT, None, exc.reason, 0, False, True
    except VenueResolutionError as exc:            # § WP10/WP11: re-selectable, budget-neutral, NOT aborting
        # Carry the machine code ('venue_unresolved') into the reason so _qualification_detail labels an
        # exception-path venue gap (unmapped derivative, bond-without-ISIN, error-200) identically to the
        # in-matcher reclassification path — otherwise the analytics column would be NULL for those rows.
        return QualificationStatus.ERROR_RETRYABLE, None, f"{exc.code}: {exc.reason}", 0, False, False
    except ConnectionUnavailableError as exc:      # broker outage: re-selectable, budget-neutral, aborting
        return QualificationStatus.ERROR_RETRYABLE, None, exc.reason, 0, True, False
    except RetryableQualificationError as exc:
        status = (QualificationStatus.ERROR_PERMANENT if this_attempt >= max_attempts
                  else QualificationStatus.ERROR_RETRYABLE)
        return status, None, exc.reason, 0, False, True
    except Exception as exc:  # noqa: BLE001 — unknown fault: conservative retry, escalate when exhausted
        status = (QualificationStatus.ERROR_PERMANENT if this_attempt >= max_attempts
                  else QualificationStatus.ERROR_RETRYABLE)
        return status, None, f"{type(exc).__name__}: {exc}", 0, False, True


def _summary(run, *, resumed: bool, connection_lost: bool) -> QualificationSummary:
    return QualificationSummary(
        run_id=run.run_id, status=run.status, processed=run.processed_count, verified=run.verified_count,
        ambiguous=run.ambiguous_count, not_tradable=run.not_tradable_count,
        market_data_not_entitled=run.mdne_count, error_retryable=run.error_retryable_count,
        error_permanent=run.error_permanent_count,
        completed_markets=json.loads(run.completed_markets_json),
        failed_markets=json.loads(run.failed_markets_json), resumed=resumed, connection_lost=connection_lost,
    )
