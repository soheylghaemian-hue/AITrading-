"""WP6 — the macro / geopolitical / regulatory source registry (seed).

Declares the target macro channel CLASSES the platform intends to cover — central banks (Fed/ECB/BoE/national),
supranational bodies (BIS/IMF/World Bank/UN/FSB), regulators, sanctions/trade authorities, and
conflict/energy/transport monitors. EVERY seed is FAIL-CLOSED: ``available=False``, license ``UNKNOWN``, and
storage/redistribution/commercial rights all ``False`` — a source only becomes active when a REAL, entitled
provider (existing legal credentials + usage rights, no new keys, no scraping) is attached. The registry
declaring a class is NOT a claim of coverage; the readmodel reports active vs MISSING sources explicitly.

SAFETY: reference/registry data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

from .model import MacroSourceClass, MacroSourceEntry

CB = MacroSourceClass.CENTRAL_BANK.value
SUP = MacroSourceClass.SUPRANATIONAL.value
REG = MacroSourceClass.NATIONAL_REGULATOR.value
SANC = MacroSourceClass.SANCTIONS_AUTHORITY.value
TRADE = MacroSourceClass.TRADE_AUTHORITY.value
CONF = MacroSourceClass.CONFLICT_MONITOR.value
ENERGY = MacroSourceClass.ENERGY_AUTHORITY.value
TRANSP = MacroSourceClass.TRANSPORT_AUTHORITY.value


def seed_sources() -> list[MacroSourceEntry]:
    """The macro/geopolitical/regulatory source CLASSES, all fail-closed (off + unlicensed) until an entitled
    provider attaches. Regions/mandate are declarative metadata, not an availability claim."""
    return [
        MacroSourceEntry("us_federal_reserve", "US Federal Reserve (FOMC/statements)", source_class=CB,
                         regions=("AMERICAS",), languages=("en",), mandate="monetary"),
        MacroSourceEntry("ecb", "European Central Bank", source_class=CB,
                         regions=("EUROPE",), languages=("en",), mandate="monetary"),
        MacroSourceEntry("bank_of_england", "Bank of England", source_class=CB,
                         regions=("EUROPE",), languages=("en",), mandate="monetary"),
        MacroSourceEntry("national_central_banks", "National central banks (aggregate)", source_class=CB,
                         regions=("GLOBAL",), languages=(), mandate="monetary"),
        MacroSourceEntry("bis", "Bank for International Settlements", source_class=SUP,
                         regions=("GLOBAL",), languages=("en",), mandate="systemic_risk"),
        MacroSourceEntry("imf", "International Monetary Fund (WEO)", source_class=SUP,
                         regions=("GLOBAL",), languages=("en",), mandate="macro_outlook"),
        MacroSourceEntry("world_bank", "World Bank", source_class=SUP,
                         regions=("GLOBAL",), languages=("en",), mandate="macro_outlook"),
        MacroSourceEntry("united_nations", "United Nations", source_class=SUP,
                         regions=("GLOBAL",), languages=("en",), mandate="geopolitics"),
        MacroSourceEntry("sanctions_authorities", "Sanctions authorities (OFAC/EU/UN)", source_class=SANC,
                         regions=("GLOBAL",), languages=("en",), mandate="sanctions"),
        MacroSourceEntry("trade_authorities", "Trade / export-control authorities", source_class=TRADE,
                         regions=("GLOBAL",), languages=("en",), mandate="trade"),
        MacroSourceEntry("national_regulators", "National financial regulators (aggregate)", source_class=REG,
                         regions=("GLOBAL",), languages=(), mandate="regulation"),
        MacroSourceEntry("conflict_monitors", "Conflict / geopolitical risk monitors", source_class=CONF,
                         regions=("GLOBAL",), languages=("en",), mandate="conflict"),
        MacroSourceEntry("energy_authorities", "Energy supply authorities (IEA/OPEC ref.)", source_class=ENERGY,
                         regions=("GLOBAL",), languages=("en",), mandate="energy"),
        MacroSourceEntry("transport_authorities", "Transport / maritime disruption monitors",
                         source_class=TRANSP, regions=("GLOBAL",), languages=("en",), mandate="transport"),
    ]


def seed_registry(store) -> int:
    """Upsert the fail-closed macro source registry. Idempotent. Returns the number of sources seeded."""
    entries = seed_sources()
    for entry in entries:
        store.mx_upsert_macro_source(entry.as_record())
    return len(entries)
