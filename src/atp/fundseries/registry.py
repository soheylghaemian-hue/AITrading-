"""WP7 — the fundamentals & macro-series source registry (seed).

Declares the target fundamentals/macro source CLASSES the platform intends to cover — national statistics
offices, central-bank data, supranational datasets (IMF/World Bank/OECD), rating agencies, company filings,
and licensed data vendors. EVERY seed is FAIL-CLOSED: ``available=False``, license ``UNKNOWN``, and
storage/redistribution/commercial rights all ``False`` — a source only becomes active when a REAL, entitled
provider (existing legal credentials + usage rights, no new keys, no scraping) is attached. The registry
declaring a class is NOT a claim of coverage; the readmodel reports active vs MISSING sources explicitly.

SAFETY: reference/registry data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

from .model import FundamentalSourceEntry, SourceType

STAT = SourceType.STATISTICS_OFFICE.value
CB = SourceType.CENTRAL_BANK.value
SUP = SourceType.SUPRANATIONAL.value
RATING = SourceType.RATING_AGENCY.value
FILINGS = SourceType.COMPANY_FILINGS.value
VENDOR = SourceType.DATA_VENDOR.value


def seed_sources() -> list[FundamentalSourceEntry]:
    """The fundamentals/macro source CLASSES, all fail-closed (off + unlicensed) until an entitled provider
    attaches. Regions are declarative metadata, not an availability claim."""
    return [
        FundamentalSourceEntry("us_bls", "US Bureau of Labor Statistics", source_type=STAT,
                               regions=("AMERICAS",), languages=("en",)),
        FundamentalSourceEntry("us_bea", "US Bureau of Economic Analysis", source_type=STAT,
                               regions=("AMERICAS",), languages=("en",)),
        FundamentalSourceEntry("eurostat", "Eurostat", source_type=STAT,
                               regions=("EUROPE",), languages=("en",)),
        FundamentalSourceEntry("national_statistics_offices", "National statistics offices (aggregate)",
                               source_type=STAT, regions=("GLOBAL",), languages=()),
        FundamentalSourceEntry("central_bank_data", "Central-bank statistical data (aggregate)",
                               source_type=CB, regions=("GLOBAL",), languages=()),
        FundamentalSourceEntry("imf_data", "IMF data (IFS/WEO)", source_type=SUP,
                               regions=("GLOBAL",), languages=("en",)),
        FundamentalSourceEntry("world_bank_data", "World Bank open data", source_type=SUP,
                               regions=("GLOBAL",), languages=("en",)),
        FundamentalSourceEntry("oecd_data", "OECD data", source_type=SUP,
                               regions=("GLOBAL",), languages=("en",)),
        FundamentalSourceEntry("rating_agencies", "Credit rating agencies (aggregate)", source_type=RATING,
                               regions=("GLOBAL",), languages=("en",)),
        FundamentalSourceEntry("company_filings", "Issuer financial statements (aggregate)",
                               source_type=FILINGS, regions=("GLOBAL",), languages=()),
        FundamentalSourceEntry("fundamentals_vendors", "Licensed fundamentals data vendors (aggregate)",
                               source_type=VENDOR, regions=("GLOBAL",), languages=()),
    ]


def seed_registry(store) -> int:
    """Upsert the fail-closed fundamentals source registry. Idempotent. Returns the number of sources seeded."""
    entries = seed_sources()
    for entry in entries:
        store.fx_upsert_source(entry.as_record())
    return len(entries)
