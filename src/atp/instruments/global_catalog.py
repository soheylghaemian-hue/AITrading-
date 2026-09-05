"""Provider-neutral global instrument catalogue.

The catalogue deliberately separates *discovery* from *permission*.  A contract may exist at
the broker without being tradeable for the account or without an entitled market-data feed.
Only records whose contract, trading permission and data permission have all been proven are
READY.  This fail-closed distinction is essential when importing large exchange listings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from ..core.enums import AssetClass


class CatalogueStatus(str, Enum):
    DISCOVERED = "discovered"
    RESOLVED = "resolved"
    SUBSCRIPTION_REQUIRED = "subscription_required"
    TRADING_PERMISSION_REQUIRED = "trading_permission_required"
    READY = "ready"
    REJECTED = "rejected"


_SEC_TYPE = {
    "STK": AssetClass.EQUITY,
    "ETF": AssetClass.ETF,
    "IND": AssetClass.INDEX,
    "CASH": AssetClass.FX,
    "BOND": AssetClass.BOND,
    "FUT": AssetClass.FUTURE,
    "CONTFUT": AssetClass.FUTURE,
    "OPT": AssetClass.OPTION,
    "FOP": AssetClass.OPTION,
    "CMDTY": AssetClass.COMMODITY,
    "CRYPTO": AssetClass.CRYPTO,
    "FUND": AssetClass.FUND,
    "WAR": AssetClass.WARRANT,
    "IOPT": AssetClass.WARRANT,
    "CERT": AssetClass.CERTIFICATE,
    "CFD": AssetClass.CFD,
}


@dataclass(frozen=True, slots=True)
class GlobalContract:
    """Canonical contract identity; ``con_id`` is the broker's stable primary key."""

    con_id: int
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    # § WP11 — the ISIN as ECHOED BACK by IBKR in ContractDetails.secIdList (NOT the requested ISIN). Empty
    # when IBKR did not return one (its presence/casing is undocumented and, for US stocks, subscription-
    # gated), so it is a fail-closed POSITIVE identity anchor only when present — never assumed.
    isin: str = ""
    country: str = ""
    description: str = ""
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    multiplier: float = 1.0
    min_tick: float | None = None
    underlying_con_id: int | None = None
    trading_allowed: bool = False
    market_data_allowed: bool = False
    delayed_data_allowed: bool = False
    status: CatalogueStatus = CatalogueStatus.DISCOVERED
    reason: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def asset_class(self) -> AssetClass | None:
        return _SEC_TYPE.get(self.sec_type.upper())

    @property
    def has_usable_data(self) -> bool:
        return self.market_data_allowed or self.delayed_data_allowed


class ContractSource(Protocol):
    """An exchange-listing, broker or reference-data source yielding canonical contracts."""

    def contracts(self) -> Iterable[GlobalContract]: ...


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    generated_at: datetime
    contracts: tuple[GlobalContract, ...]

    @property
    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in CatalogueStatus}
        for contract in self.contracts:
            result[contract.status.value] += 1
        return result


class GlobalInstrumentCatalogue:
    """Deduplicating, fail-closed catalogue for arbitrarily large listing imports."""

    def __init__(self) -> None:
        self._contracts: dict[int, GlobalContract] = {}

    def ingest(self, contracts: Iterable[GlobalContract]) -> int:
        changed = 0
        for contract in contracts:
            checked = self._validate(contract)
            previous = self._contracts.get(checked.con_id)
            if previous is not None and replace(checked, observed_at=previous.observed_at) == previous:
                continue
            if previous != checked:
                self._contracts[checked.con_id] = checked
                changed += 1
        return changed

    def ingest_source(self, source: ContractSource) -> int:
        return self.ingest(source.contracts())

    def set_permissions(
        self,
        con_id: int,
        *,
        trading_allowed: bool,
        market_data_allowed: bool,
        delayed_data_allowed: bool = False,
        reason: str = "",
    ) -> GlobalContract:
        contract = self._contracts[con_id]
        if not trading_allowed:
            status = CatalogueStatus.TRADING_PERMISSION_REQUIRED
        elif not (market_data_allowed or delayed_data_allowed):
            status = CatalogueStatus.SUBSCRIPTION_REQUIRED
        else:
            status = CatalogueStatus.READY
        updated = replace(
            contract,
            trading_allowed=trading_allowed,
            market_data_allowed=market_data_allowed,
            delayed_data_allowed=delayed_data_allowed,
            status=status,
            reason=reason,
            observed_at=datetime.now(UTC),
        )
        self._contracts[con_id] = updated
        return updated

    def snapshot(self) -> CatalogueSnapshot:
        contracts = tuple(sorted(self._contracts.values(), key=lambda item: item.con_id))
        return CatalogueSnapshot(datetime.now(UTC), contracts)

    def ready(self, asset_class: AssetClass | None = None) -> list[GlobalContract]:
        return [
            item for item in self._contracts.values()
            if item.status is CatalogueStatus.READY
            and (asset_class is None or item.asset_class is asset_class)
        ]

    @staticmethod
    def _validate(contract: GlobalContract) -> GlobalContract:
        reason = ""
        if contract.con_id <= 0:
            reason = "missing stable broker contract id"
        elif not contract.symbol.strip():
            reason = "missing symbol"
        elif contract.asset_class is None:
            reason = f"unsupported security type: {contract.sec_type}"
        elif not contract.exchange.strip():
            reason = "missing exchange"
        elif not contract.currency.strip():
            reason = "missing currency"
        elif contract.sec_type.upper() in {"FUT", "FOP", "OPT"} and not contract.expiry:
            reason = "derivative missing expiry"
        if reason:
            return replace(contract, status=CatalogueStatus.REJECTED, reason=reason)
        if contract.status is CatalogueStatus.DISCOVERED:
            return replace(contract, status=CatalogueStatus.RESOLVED)
        return contract
