"""IBKR reference-data adapter for the global instrument catalogue.

IBKR does not expose a single "download every contract" API.  Listing identifiers must come
from exchange/reference-data sources and are then qualified here in bounded batches.  Keeping
that constraint explicit prevents a representative scanner result from being mislabeled as a
complete world universe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .global_catalog import GlobalContract


def _isin_from_sec_id_list(detail: Any) -> str:
    """§ WP11 — extract the ISIN that IBKR ECHOED in ``ContractDetails.secIdList`` (a list of TagValue-like
    objects). Fail-closed: the tag string/casing is not documented by IBKR and, for US stocks, the ISIN is
    only present with a CUSIP market-data subscription — so an absent/unparseable list yields ``""`` (never a
    fabricated identifier), and a present ISIN is matched case-insensitively on the tag with the value
    normalized (strip/upper). This is a POSITIVE identity anchor only when present."""
    sec_id_list = getattr(detail, "secIdList", None) or []
    try:
        for entry in sec_id_list:
            tag = str(getattr(entry, "tag", "") or "").strip().upper()
            if tag == "ISIN":
                value = str(getattr(entry, "value", "") or "").strip().upper()
                if value:
                    return value
    except TypeError:   # not iterable / unexpected shape → treat as absent (fail-closed)
        return ""
    return ""


def contract_detail_to_global(detail: Any) -> GlobalContract:
    contract = detail.contract
    sec_type = str(getattr(contract, "secType", "") or "")
    stock_type = str(getattr(detail, "stockType", "") or "").upper()
    if sec_type == "STK" and stock_type in {"ETF", "ETP"}:
        sec_type = "ETF"
    multiplier_raw = getattr(contract, "multiplier", "") or "1"
    try:
        multiplier = float(multiplier_raw)
    except (TypeError, ValueError):
        multiplier = 1.0
    under_con_id = int(getattr(contract, "underConId", 0) or 0) or None
    return GlobalContract(
        con_id=int(getattr(contract, "conId", 0) or 0),
        symbol=str(getattr(contract, "symbol", "") or ""),
        local_symbol=str(getattr(contract, "localSymbol", "") or ""),
        sec_type=sec_type,
        exchange=str(getattr(contract, "exchange", "") or ""),
        primary_exchange=str(getattr(contract, "primaryExchange", "") or ""),
        currency=str(getattr(contract, "currency", "") or ""),
        isin=_isin_from_sec_id_list(detail),
        country=str(getattr(detail, "country", "") or ""),
        description=str(getattr(detail, "longName", "") or ""),
        expiry=str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        strike=_optional_float(getattr(contract, "strike", None)),
        right=str(getattr(contract, "right", "") or ""),
        multiplier=multiplier,
        min_tick=_optional_float(getattr(detail, "minTick", None)),
        underlying_con_id=under_con_id,
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class QualificationResult:
    requested: int
    resolved: tuple[GlobalContract, ...]
    unresolved: tuple[str, ...]


class IBKRContractQualifier:
    """Qualify listing contracts against a connected ``ib_async.IB`` client."""

    def __init__(
        self,
        ib: Any,
        *,
        batch_size: int = 50,
        contract_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._ib = ib
        self._batch_size = batch_size
        self._contract_factory = contract_factory or _ib_contract

    async def qualify(self, candidates: Iterable[Any]) -> QualificationResult:
        items = list(candidates)
        resolved: dict[int, GlobalContract] = {}
        unresolved: list[str] = []
        for batch in _batches(items, self._batch_size):
            for candidate in batch:
                try:
                    request = self._contract_factory(candidate)
                except ValueError:   # § WP10: unmapped venue / no usable identifier → unresolved, not a crash
                    unresolved.append(_candidate_label(candidate))
                    continue
                details = await self._ib.reqContractDetailsAsync(request)
                if not details:
                    unresolved.append(_candidate_label(candidate))
                    continue
                for detail in details:
                    record = contract_detail_to_global(detail)
                    if record.con_id > 0:
                        resolved[record.con_id] = record
        return QualificationResult(len(items), tuple(resolved.values()), tuple(unresolved))

    async def qualify_stream(self, candidates: Iterable[Any]) -> AsyncIterator[GlobalContract]:
        """Memory-bounded variant for large exchange listing files."""
        for batch in _batches(candidates, self._batch_size):
            for candidate in batch:
                try:
                    request = self._contract_factory(candidate)
                except ValueError:   # § WP10: unmapped venue / no usable identifier → skip, not a crash
                    continue
                for detail in await self._ib.reqContractDetailsAsync(request):
                    record = contract_detail_to_global(detail)
                    if record.con_id > 0:
                        yield record


def _batches(items: Iterable[Any], size: int):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _candidate_label(candidate: Any) -> str:
    symbol = str(getattr(candidate, "symbol", "") or "?")
    sec_type = str(getattr(candidate, "secType", "") or getattr(candidate, "sec_type", "") or "?")
    exchange = str(getattr(candidate, "exchange", "") or "?")
    return f"{sec_type}:{symbol}@{exchange}"


def _ib_contract(candidate: Any) -> Any:
    """§ WP10 — build an IBKR query WITHOUT the raw-MIC / ISIN-in-symbol bugs that made the qualification
    canary misread venue-resolution failures as NOT_TRADABLE. ISIN-first for FIRDS-style candidates; a real
    ticker on an already-IBKR venue (US listing sources emit NASDAQ/ARCA, not MICs) is kept as a symbol
    query; a raw ISO MIC is translated via the fail-closed venue registry. An unmapped derivative venue or a
    candidate with no usable identifier raises ValueError, which the qualifier records as 'unresolved'
    instead of sending a bad destination."""
    if hasattr(candidate, "secType"):
        return candidate
    from .ibkr_venue import is_ibkr_exchange, resolve_ibkr_exchanges

    raw = getattr(candidate, "sec_type", "") or ""
    sec_type = "STK" if raw == "ETF" else raw
    is_derivative = sec_type in ("FUT", "OPT", "FOP")
    venue = getattr(candidate, "exchange", "") or getattr(candidate, "primary_exchange", "") or ""
    isin = getattr(candidate, "isin", None) or ""
    con_id = getattr(candidate, "con_id", None)
    symbol = getattr(candidate, "symbol", "") or ""
    mapped = resolve_ibkr_exchanges(venue)

    # § WP11 — BOND: IBKR expects the CUSIP/ISIN in Contract.symbol with secType='BOND' (NOT secIdType/secId).
    # The raw MIC is never a valid destination, so route via SMART. Requires an ISIN identifier.
    if sec_type == "BOND":
        if not isin:
            raise ValueError("venue_unresolved: no ISIN for a BOND security-definition query")
        import ib_async  # lazy: no broker SDK at module load, and only after the query is validated
        return ib_async.Contract(secType="BOND", symbol=isin, exchange="SMART", currency=candidate.currency)

    kwargs: dict[str, Any] = {"secType": sec_type, "currency": candidate.currency}
    # --- venue: never a raw ISO MIC ---
    if is_derivative:
        ibkr_ex = mapped or ((venue,) if is_ibkr_exchange(venue) else ())
        if not ibkr_ex:
            raise ValueError(f"venue_unresolved: no IBKR exchange for {venue!r} ({sec_type})")
        kwargs["exchange"] = ibkr_ex[0]
    else:
        kwargs["exchange"] = "SMART"
        prim = mapped[0] if mapped else (venue if is_ibkr_exchange(venue) else "")
        if prim:
            kwargs["primaryExchange"] = prim
    # --- identity: conId > ISIN > (ticker on a known IBKR venue); never symbol==ISIN with a raw MIC ---
    if con_id:
        kwargs["conId"] = con_id
    elif isin:
        kwargs["secIdType"] = "ISIN"
        kwargs["secId"] = isin
    elif symbol and not is_derivative and is_ibkr_exchange(venue):
        kwargs["symbol"] = symbol
    else:
        raise ValueError("venue_unresolved: no ISIN/conId/ticker for a reliable IBKR query")
    for field, key in (("expiry", "lastTradeDateOrContractMonth"), ("strike", "strike"),
                       ("option_right", "right")):
        val = getattr(candidate, field, None)
        if val:
            kwargs[key] = val
    import ib_async  # lazy: no broker SDK at module load, and only after the query is validated
    return ib_async.Contract(**kwargs)
