"""WP9 — the instrument-directory SOURCE REGISTRY (declared provenance, fail-closed).

This registry declares the *official* instrument-directory source CLASSES the platform intends to bootstrap
the global catalogue from — with documented provenance, license status, update interval, regions, trading
venues (MICs / exchange codes) and asset classes. It is the instrument analogue of the news source registry
(`atp.newsroom.registry`) and it is deliberately **fail-closed**:

  * every declared source defaults to ``available=False`` — declaring a source does NOT claim coverage of it,
    and nothing is imported from a source until a real, entitled provider is actually attached and the source
    is explicitly activated;
  * a source whose license or entitlement is unclear is marked ``BLOCKED`` (with a documented reason) — it is
    surfaced by the read-model as MISSING/BLOCKED, never silently treated as covered;
  * license fields (storage / redistribution / commercial) default to the most conservative value and are
    only set to what the source's published terms actually permit.

The metadata here is documented reference fact (official homepages / registries), never fabricated. This
module performs NO network I/O, NO download and NO trading — it is a pure declarative registry.

SAFETY: reference/registry data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from ..core.enums import AssetClass


class InstrumentSourceType(str, Enum):
    """How a directory source is produced (drives trust/primacy expectations)."""

    OFFICIAL_REGULATOR = "official_regulator"      # regulator-published reference data (e.g. ESMA/FCA FIRDS)
    EXCHANGE_DIRECTORY = "exchange_directory"      # an exchange's own listed-securities directory
    REGULATORY_FILING = "regulatory_filing"        # a filing system's issuer/ticker registry (e.g. SEC)
    BROKER_QUALIFIER = "broker_qualifier"          # a broker contract-details verification path (IBKR)


class InstrumentSourceLicense(str, Enum):
    """The license basis under which a directory MAY be used — fail-closed default UNKNOWN."""

    UNKNOWN = "unknown"                            # not established → fail-closed, treat as BLOCKED
    PUBLIC_DOMAIN = "public_domain"                # e.g. a US-government work (SEC), no copyright
    PUBLIC_REUSE = "public_reuse"                  # published for public reuse (attribution may apply)
    ATTRIBUTION_REQUIRED = "attribution_required"  # reuse permitted with source acknowledgement
    BLOCKED = "blocked"                            # terms prohibit our use / unconfirmed → do not use


class InstrumentSourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"    # an entitled provider is attached and the source is activated
    MISSING = "MISSING"        # declared but no provider attached yet (fail-closed default)
    BLOCKED = "BLOCKED"        # license/entitlement unclear or prohibited → must not be used


@dataclass(frozen=True, slots=True)
class InstrumentSourceEntry:
    """One declared instrument-directory source with its documented provenance and usage rights."""

    source_id: str
    name: str
    source_type: str                       # InstrumentSourceType value
    provenance_url: str                    # documented official homepage / spec (evidence of origin)
    regions: tuple = ()                    # e.g. ("EUROPE",) / ("AMERICAS", "US")
    venues: tuple = ()                     # MICs / exchange codes the source covers (() = broad/unspecified)
    asset_classes: tuple = ()              # AssetClass values the source can supply
    update_mode: str = "unknown"           # daily / monthly / on_demand / poll / unknown
    license_status: str = InstrumentSourceLicense.UNKNOWN.value
    storage_allowed: bool = False
    redistribution_allowed: bool = False
    commercial_use_allowed: bool = False
    attribution_required: bool = True
    available: bool = False                # fail-closed: no provider attached / not activated
    blocked_reason: str = ""               # non-empty ⇒ BLOCKED (never use without new authorization)

    @property
    def status(self) -> str:
        # Truthfulness invariant: a block or an unknown/blocked license is BLOCKED *regardless* of the
        # `available` flag — flipping availability must never make a blocked source look covered.
        if self.blocked_reason or self.license_status in (
            InstrumentSourceLicense.UNKNOWN.value, InstrumentSourceLicense.BLOCKED.value,
        ):
            return InstrumentSourceStatus.BLOCKED.value
        return InstrumentSourceStatus.AVAILABLE.value if self.available else InstrumentSourceStatus.MISSING.value

    @property
    def usable(self) -> bool:
        """A source may be imported from ONLY when it is available AND not blocked AND its license permits at
        least local storage. Fail-closed: any unknown/blocked license ⇒ not usable."""
        return (
            self.available
            and not self.blocked_reason
            and self.license_status not in (InstrumentSourceLicense.UNKNOWN.value,
                                            InstrumentSourceLicense.BLOCKED.value)
            and self.storage_allowed
        )

    def summary(self) -> dict:
        return {
            "source_id": self.source_id, "name": self.name, "source_type": self.source_type,
            "provenance_url": self.provenance_url, "status": self.status, "available": self.available,
            "usable": self.usable, "regions": list(self.regions), "venues": list(self.venues),
            "asset_classes": list(self.asset_classes), "update_mode": self.update_mode,
            "license": {"license_status": self.license_status, "storage_allowed": self.storage_allowed,
                        "redistribution_allowed": self.redistribution_allowed,
                        "commercial_use_allowed": self.commercial_use_allowed,
                        "attribution_required": self.attribution_required},
            "blocked_reason": self.blocked_reason or None,
        }

    def as_json(self) -> str:
        return json.dumps(self.summary(), sort_keys=True, separators=(",", ":"))


_ALL = tuple(a.value for a in AssetClass)
_EQ = (AssetClass.EQUITY.value,)
_EQ_ETF = (AssetClass.EQUITY.value, AssetClass.ETF.value)
# FIRDS carries every MiFID-II-reportable instrument admitted to an EU/UK trading venue.
_FIRDS_CLASSES = (
    AssetClass.EQUITY.value, AssetClass.ETF.value, AssetClass.FUND.value, AssetClass.BOND.value,
    AssetClass.OPTION.value, AssetClass.FUTURE.value, AssetClass.WARRANT.value,
    AssetClass.CERTIFICATE.value, AssetClass.INDEX.value,
)


def seed_sources() -> list[InstrumentSourceEntry]:
    """The declared instrument-directory source classes. All fail-closed (``available=False``) until a real,
    entitled provider is attached — declaring a source is not a claim of coverage."""
    return [
        # -- EU/UK official regulator reference data (the multi-asset workhorse) --------------------------
        InstrumentSourceEntry(
            source_id="esma_firds", name="ESMA FIRDS (Financial Instruments Reference Data System)",
            source_type=InstrumentSourceType.OFFICIAL_REGULATOR.value,
            provenance_url="https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_firds",
            regions=("EUROPE",), venues=(), asset_classes=_FIRDS_CLASSES, update_mode="daily",
            # ESMA publishes FIRDS reference data for public reuse; ESMA's legal notice permits reproduction
            # with source acknowledgement. Commercial redistribution should be reconfirmed before enabling.
            license_status=InstrumentSourceLicense.ATTRIBUTION_REQUIRED.value,
            storage_allowed=True, redistribution_allowed=False, commercial_use_allowed=False,
            attribution_required=True, available=False),
        InstrumentSourceEntry(
            source_id="fca_firds", name="FCA FIRDS (UK Financial Instruments Reference Data System)",
            source_type=InstrumentSourceType.OFFICIAL_REGULATOR.value,
            provenance_url="https://www.fca.org.uk/markets/market-data-regimes/financial-instruments-transparency-system",
            regions=("EUROPE", "GB"), venues=("XLON",), asset_classes=_FIRDS_CLASSES, update_mode="daily",
            license_status=InstrumentSourceLicense.ATTRIBUTION_REQUIRED.value,
            storage_allowed=True, redistribution_allowed=False, commercial_use_allowed=False,
            attribution_required=True, available=False),
        # -- US public directories --------------------------------------------------------------------------
        InstrumentSourceEntry(
            source_id="sec_company_tickers", name="SEC EDGAR company_tickers(_exchange)",
            source_type=InstrumentSourceType.REGULATORY_FILING.value,
            provenance_url="https://www.sec.gov/files/company_tickers_exchange.json",
            regions=("AMERICAS", "US"), venues=(), asset_classes=_EQ, update_mode="daily",
            # US-government work → no copyright (public domain). Fair-access policy requires a descriptive
            # User-Agent (ATP_SEC_USER_AGENT); no key, no paywall.
            license_status=InstrumentSourceLicense.PUBLIC_DOMAIN.value,
            storage_allowed=True, redistribution_allowed=True, commercial_use_allowed=True,
            attribution_required=False, available=False),
        InstrumentSourceEntry(
            source_id="nasdaq_trader", name="Nasdaq Trader Symbol Directory (nasdaqlisted/otherlisted)",
            source_type=InstrumentSourceType.EXCHANGE_DIRECTORY.value,
            provenance_url="https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs",
            regions=("AMERICAS", "US"), venues=("XNAS", "XNYS", "ARCX", "XASE", "BATS", "IEXG"),
            asset_classes=_EQ_ETF, update_mode="daily",
            license_status=InstrumentSourceLicense.PUBLIC_REUSE.value,
            storage_allowed=True, redistribution_allowed=False, commercial_use_allowed=False,
            attribution_required=True, available=False),
        # -- Asia-Pacific: declared but license/redistribution unconfirmed ⇒ BLOCKED (fail-closed) ----------
        InstrumentSourceEntry(
            source_id="jpx_listed_issues", name="JPX — List of TSE-listed Issues",
            source_type=InstrumentSourceType.EXCHANGE_DIRECTORY.value,
            provenance_url="https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html",
            regions=("APAC", "JP"), venues=("XTKS",), asset_classes=_EQ, update_mode="monthly",
            license_status=InstrumentSourceLicense.UNKNOWN.value,
            attribution_required=True, available=False,
            blocked_reason="Free monthly list exists, but JPX reuse/redistribution terms are unconfirmed."),
        InstrumentSourceEntry(
            source_id="asx_listed", name="ASX — listed companies directory",
            source_type=InstrumentSourceType.EXCHANGE_DIRECTORY.value,
            provenance_url="https://www.asx.com.au/markets/trade-our-cash-market/directory",
            regions=("APAC", "AU"), venues=("XASX",), asset_classes=_EQ, update_mode="unknown",
            license_status=InstrumentSourceLicense.UNKNOWN.value, attribution_required=True, available=False,
            blocked_reason="ASX market-data terms restrict redistribution; license not confirmed."),
        InstrumentSourceEntry(
            source_id="hkex_listed", name="HKEX — list of securities",
            source_type=InstrumentSourceType.EXCHANGE_DIRECTORY.value,
            provenance_url="https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities",
            regions=("APAC", "HK"), venues=("XHKG",), asset_classes=_EQ, update_mode="unknown",
            license_status=InstrumentSourceLicense.UNKNOWN.value, attribution_required=True, available=False,
            blocked_reason="HKEX securities list terms restrict reuse; license not confirmed."),
        InstrumentSourceEntry(
            source_id="sgx_listed", name="SGX — securities directory",
            source_type=InstrumentSourceType.EXCHANGE_DIRECTORY.value,
            provenance_url="https://www.sgx.com/securities/securities-prices",
            regions=("APAC", "SG"), venues=("XSES",), asset_classes=_EQ, update_mode="unknown",
            license_status=InstrumentSourceLicense.UNKNOWN.value, attribution_required=True, available=False,
            blocked_reason="SGX securities directory reuse terms are unconfirmed."),
        # -- IBKR read-only qualifier (verification path, not an importer of new symbols) -------------------
        InstrumentSourceEntry(
            source_id="ibkr_qualifier",
            name="Interactive Brokers reqContractDetails (read-only qualification)",
            source_type=InstrumentSourceType.BROKER_QUALIFIER.value,
            provenance_url="https://ibkrcampus.com/ibkr-api-page/twsapi-doc/#contract-details",
            regions=("GLOBAL",), venues=(), asset_classes=_ALL, update_mode="on_demand",
            # No external data license — this is the account's own broker entitlement, read-only, no data buy.
            license_status=InstrumentSourceLicense.PUBLIC_REUSE.value,
            storage_allowed=True, redistribution_allowed=False, commercial_use_allowed=False,
            attribution_required=False, available=False,
            blocked_reason="Requires an entitled IBKR gateway session; disabled in this build."),
    ]


def source_by_id(source_id: str) -> InstrumentSourceEntry | None:
    for entry in seed_sources():
        if entry.source_id == source_id:
            return entry
    return None
