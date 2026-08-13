"""Global instrument universe (§ Phase 10) — provider-independent contract specs.

A representative multi-region Level-1 universe. Each spec carries the IBKR routing hints
(exchange / primary_exchange / currency) so the Contract Resolver can build a contract. The
system is NOT limited to AAPL/NVDA/SPY — instruments auto-transition SUBSCRIPTION_REQUIRED → READY
when their market-data subscription becomes active, with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import AssetClass


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    region: str
    exchange: str            # IBKR routing (SMART or a specific venue)
    primary_exchange: str    # listing venue (disambiguates SMART)
    currency: str
    asset_class: AssetClass = AssetClass.EQUITY
    label: str = ""          # human name / venue label for the dashboard


def _eq(sym, region, primary, ccy, exch="SMART", label=""):
    return InstrumentSpec(sym, region, exch, primary, ccy, AssetClass.EQUITY, label)


def _fx(pair):
    base, quote = pair.split(".")
    return InstrumentSpec(pair, "FX", "IDEALPRO", "IDEALPRO", quote, AssetClass.FX, "IDEALPRO FX")


# Representative, broad (not exhaustive) global Level-1 universe.
GLOBAL_UNIVERSE: list[InstrumentSpec] = [
    # --- USA (NASDAQ/NYSE/ARCA) --------------------------------------------
    _eq("AAPL", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("NVDA", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("MSFT", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("AMZN", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("META", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("GOOGL", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("TSLA", "USA", "NASDAQ", "USD", label="NASDAQ"),
    _eq("SPY", "USA", "ARCA", "USD", label="ARCA"),
    # --- Germany (Xetra / Stuttgart) ---------------------------------------
    _eq("SAP", "Germany", "IBIS", "EUR", label="XETRA"),
    _eq("SIE", "Germany", "IBIS", "EUR", label="XETRA"),
    _eq("ALV", "Germany", "IBIS", "EUR", label="XETRA"),
    # --- UK (LSE) ----------------------------------------------------------
    _eq("VOD", "UK", "LSE", "GBP", label="LSE"),
    _eq("HSBA", "UK", "LSE", "GBP", label="LSE"),
    # --- Switzerland (SIX) -------------------------------------------------
    _eq("NESN", "Switzerland", "EBS", "CHF", label="SIX"),
    _eq("NOVN", "Switzerland", "EBS", "CHF", label="SIX"),
    # --- France (Euronext Paris) -------------------------------------------
    _eq("MC", "France", "SBF", "EUR", label="Euronext Paris"),
    _eq("OR", "France", "SBF", "EUR", label="Euronext Paris"),
    # --- Italy (Borsa Italiana) --------------------------------------------
    _eq("ISP", "Italy", "BVME", "EUR", label="Borsa Italiana"),
    # --- Spain (Bolsa de Madrid) -------------------------------------------
    _eq("SAN", "Spain", "BM", "EUR", label="Bolsa de Madrid"),
    _eq("ITX", "Spain", "BM", "EUR", label="Bolsa de Madrid"),
    # --- Nordics (Stockholm / Helsinki) ------------------------------------
    _eq("VOLV.B", "Nordics", "SFB", "SEK", label="Nasdaq Stockholm"),
    _eq("NOKIA", "Nordics", "HEX", "EUR", label="Nasdaq Helsinki"),
    # --- Austria (Vienna) --------------------------------------------------
    _eq("OMV", "Austria", "VSE", "EUR", label="Wiener Börse"),
    # --- Canada (TSX) ------------------------------------------------------
    _eq("RY", "Canada", "TSE", "CAD", label="TSX"),
    _eq("SHOP", "Canada", "TSE", "CAD", label="TSX"),
    # --- Japan (Tokyo) -----------------------------------------------------
    _eq("7203", "Japan", "TSEJ", "JPY", label="Tokyo SE"),   # Toyota
    # --- Australia (ASX) ---------------------------------------------------
    _eq("BHP", "Australia", "ASX", "AUD", label="ASX"),
    # --- Singapore (SGX) ---------------------------------------------------
    _eq("D05", "Singapore", "SGX", "SGD", label="SGX"),
    # --- FX (IDEALPRO) -----------------------------------------------------
    _fx("EUR.USD"),
    _fx("USD.JPY"),
    _fx("GBP.USD"),
]
