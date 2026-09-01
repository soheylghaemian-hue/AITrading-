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
    """Qualify listing contracts against a connected ``ib_insync.IB`` client."""

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
                request = self._contract_factory(candidate)
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
                request = self._contract_factory(candidate)
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
    if hasattr(candidate, "secType"):
        return candidate
    import ib_insync

    sec_type = "STK" if getattr(candidate, "sec_type", "") == "ETF" else candidate.sec_type
    return ib_insync.Contract(
        symbol=candidate.symbol,
        secType=sec_type,
        exchange="SMART" if sec_type == "STK" else candidate.exchange,
        primaryExchange=candidate.exchange if sec_type == "STK" else "",
        currency=candidate.currency,
    )
