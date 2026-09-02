"""WP9 — real read-only adapters for official instrument directories + a deterministic stub.

Parsers that turn an *already-obtained* official directory file into the platform's `ListingCandidate`
records, plus a narrow read-only `DirectoryProvider` interface (cursor-paginated) and a network-free
`StubDirectoryProvider` for tests/CI. Concretely:

  * `parse_firds_fulins` — ESMA/FCA FIRDS FULINS reference data (ISO-20022 XML). ISIN, venue MIC, notional
    currency, name and the derivative-identity fields (expiry / strike / option right / underlying) are read
    straight from the record; the CFI (ISO-10962) classification maps to an asset class. An instrument with
    no ISIN, no venue MIC or an unmappable CFI is SKIPPED — never guessed.
  * `parse_sec_company_tickers` — SEC EDGAR `company_tickers_exchange.json` (US issuers; ticker + exchange).
  * the existing NASDAQ Trader parsers (`atp.instruments.listing_sources`) are reused for US listings.

**No network at import and no download here.** These functions parse data handed to them; obtaining the file
(a live HTTP fetch) is a separate, deliberately-disabled concern gated behind the fail-closed source registry
(`atp.instruments.sources`). Only stdlib (`xml`, `json`, `io`) and local modules are imported.

SAFETY: reference data only. No trading, no orders/execution/broker, no market-data subscription. Unknown
values stay NULL — never fabricated. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import abc
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from .importer import ListingProvider, MarketPlan, MarketSource
from .listing_sources import ListingCandidate


# --------------------------------------------------------------------------- read-only provider interface
class DirectoryProviderError(Exception):
    def __init__(self, message: str, *, code: str = "DIRECTORY_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class DirectoryUnavailableError(DirectoryProviderError):
    def __init__(self, message: str, *, code: str = "DIRECTORY_UNAVAILABLE") -> None:
        super().__init__(message, code=code)


class DirectoryRateLimitedError(DirectoryProviderError):
    def __init__(self, message: str, *, code: str = "DIRECTORY_RATE_LIMITED") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class DirectoryPage:
    candidates: tuple = ()                 # tuple[ListingCandidate, ...]
    next_cursor: str | None = None         # None → no more pages (resumable cursor)


@dataclass(frozen=True, slots=True)
class DirectoryProviderStatus:
    available: bool
    reason: str = ""
    rate_limited: bool = False


class DirectoryProvider(abc.ABC):
    """A read-only official-directory provider. Implementations expose ONLY a capability/status probe and a
    cursor-paginated fetch of listing candidates — no order/execution/account/subscription method."""

    source_id: str = "directory"

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True only when the provider can actually serve data (an entitled/local source is present)."""

    @abc.abstractmethod
    def capabilities(self) -> tuple:
        ...

    @abc.abstractmethod
    def provider_status(self) -> DirectoryProviderStatus:
        """A read-only availability check — no side effects, no subscription purchase."""

    @abc.abstractmethod
    def fetch_candidates(self, *, cursor: str | None = None, limit: int = 1000) -> DirectoryPage:
        """Fetch a page of listing candidates from `cursor`. Returns candidates + the next cursor."""


@dataclass
class StubDirectoryProvider(DirectoryProvider):
    """Deterministic, network-free provider for tests/CI. Serves the fixture pages it is given, paginating by
    an integer cursor. A real network provider may only be added when EXISTING legal rights apply (no new
    keys, no ToS/paywall bypass, no scraping) and never runs in CI."""

    source_id: str = "stub-directory"
    pages: list = field(default_factory=list)     # list[list[ListingCandidate]] — one per page
    available: bool = True
    rate_limited: bool = False
    unavailable: bool = False
    calls: list = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return True

    def capabilities(self) -> tuple:
        return ("fetch_candidates", "cursor")

    def provider_status(self) -> DirectoryProviderStatus:
        return DirectoryProviderStatus(available=self.available and not self.unavailable,
                                       reason=("unavailable" if self.unavailable else ""),
                                       rate_limited=self.rate_limited)

    def fetch_candidates(self, *, cursor: str | None = None, limit: int = 1000) -> DirectoryPage:
        self.calls.append(cursor)
        if self.unavailable:
            raise DirectoryUnavailableError("stub: directory unavailable")
        if self.rate_limited:
            raise DirectoryRateLimitedError("stub: rate limited")
        idx = 0 if cursor is None else int(cursor)
        if idx >= len(self.pages):
            return DirectoryPage(candidates=(), next_cursor=None)
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
        return DirectoryPage(candidates=tuple(self.pages[idx]), next_cursor=nxt)


def directory_to_provider(provider: DirectoryProvider, *, page_limit: int = 1000,
                          max_pages: int = 100_000) -> ListingProvider:
    """Bridge a cursor-paginated `DirectoryProvider` into the importer's `ListingProvider` (a lazy callable
    that yields all candidates). Draining the cursor here keeps the importer's per-market resume/isolation
    intact while the provider streams pages. Raising propagates as a market failure (isolated)."""
    def _provide() -> list[ListingCandidate]:
        out: list[ListingCandidate] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = provider.fetch_candidates(cursor=cursor, limit=page_limit)
            out.extend(page.candidates)
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor
        return out
    return _provide


def provider_from_candidates(candidates: list[ListingCandidate]) -> ListingProvider:
    """A `ListingProvider` backed by already-parsed candidates (no network)."""
    frozen = list(candidates)
    return lambda: list(frozen)


# --------------------------------------------------------------------------- FIRDS (ISO-20022) parser
# ISO-10962 CFI first character → the platform's broker/reference sec_type. Conservative: an unmapped
# category returns None and the record is skipped (never classified into a guessed asset class).
def cfi_to_sec_type(cfi: str | None) -> str | None:
    c = (cfi or "").strip().upper()
    if not c:
        return None
    cat = c[0]
    grp = c[1] if len(c) > 1 else ""
    if cat == "E":                       # Equities
        return "STK"
    if cat == "C":                       # Collective investment vehicles
        return "ETF" if grp == "E" else "FUND"   # CE… = exchange-traded fund; otherwise a fund
    if cat == "D":                       # Debt instruments
        return "BOND"
    if cat == "O":                       # Listed options
        return "OPT"
    if cat == "H":                       # Non-listed & complex listed options
        return "OPT"
    if cat == "F":                       # Futures
        return "FUT"
    if cat == "R":                       # Entitlements (rights / warrants)
        return "WAR"
    return None                          # S/I/J/K/L/M/T… → not classified here → skipped


def _ln(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    if el is None:
        return None
    for c in list(el):
        if _ln(c) == name:
            return c
    return None


def _deep(el, name: str):
    if el is None:
        return None
    for c in el.iter():
        if _ln(c) == name:
            return c
    return None


def _txt(el) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _decimal_text(value: str | None) -> str | None:
    if not value:
        return None
    try:
        d = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return format(d.normalize(), "f")


def parse_firds_fulins(handle: TextIO | str | Path) -> list[ListingCandidate]:
    """Parse a FIRDS FULINS reference-data document (ISO-20022 XML) into listing candidates.

    Robust to schema/namespace variation: elements are matched by local name. From each ``RefData`` record we
    read ISIN, name, CFI (→ asset class), venue MIC (→ exchange) and notional currency; for derivatives we
    also read expiry, strike, option type and the underlying ISIN. A record missing its ISIN, its venue MIC or
    with an unmappable CFI is skipped (never fabricated)."""
    if isinstance(handle, (str, Path)) and not (isinstance(handle, str) and handle.lstrip().startswith("<")):
        with Path(handle).open(encoding="utf-8") as fh:
            root = ET.parse(fh).getroot()
    elif isinstance(handle, str):
        root = ET.fromstring(handle)
    else:
        root = ET.parse(handle).getroot()

    out: list[ListingCandidate] = []
    for ref in (el for el in root.iter() if _ln(el) == "RefData"):
        gen = _child(ref, "FinInstrmGnlAttrbts")
        isin = _txt(_child(gen, "Id"))
        cfi = _txt(_child(gen, "ClssfctnTp"))
        sec_type = cfi_to_sec_type(cfi)
        tvattr = _child(ref, "TradgVnRltdAttrbts")
        mic = _txt(_child(tvattr, "Id"))
        currency = _txt(_child(gen, "NtnlCcy"))
        # currency is part of the natural key — a record without a notional currency is skipped, never
        # defaulted (no-fabrication), alongside a missing ISIN / venue MIC / unmappable CFI.
        if not isin or not mic or sec_type is None or not currency:
            continue
        name = _txt(_child(gen, "FullNm")) or _txt(_child(gen, "ShrtNm")) or ""

        expiry = strike = right = underlying = multiplier = None
        deriv = _child(ref, "DerivInstrmAttrbts")
        if deriv is not None:
            expiry = _txt(_child(deriv, "XpryDt"))
            multiplier = _decimal_text(_txt(_child(deriv, "PricMltplr")))
            opt = _txt(_child(deriv, "OptnTp"))
            if opt:
                right = {"PUT": "P", "CALL": "C"}.get(opt.strip().upper(), "")
                right = right or None
            strike = _decimal_text(_txt(_deep(_child(deriv, "StrkPric"), "Amt")))
            underlying = _txt(_deep(_child(deriv, "UndrlygInstrm"), "ISIN"))

        out.append(ListingCandidate(
            symbol=isin,                                   # FIRDS is ISIN-centric; ISIN is the stable id
            sec_type=sec_type,
            exchange=mic.strip().upper(),                  # venue MIC (ISO-10383) — a REAL venue, never SMART
            currency=currency.strip().upper(),
            description=name,
            source="FIRDS",
            isin=isin,
            primary_exchange=mic.strip().upper(),
            expiry=expiry,
            strike=strike,
            option_right=right,
            underlying_symbol=underlying,
            multiplier=multiplier,
        ))
    return out


def read_firds_fulins(path: str | Path) -> list[ListingCandidate]:
    with Path(path).open(encoding="utf-8") as handle:
        return parse_firds_fulins(handle)


# --------------------------------------------------------------------------- SEC company_tickers parser
_SEC_EXCHANGE = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSE ARCA": "ARCA", "NYSEARCA": "ARCA",
                 "NYSE AMERICAN": "NYSEAMER", "NYSEAMERICAN": "NYSEAMER", "CBOE": "CBOE", "BATS": "BATS",
                 "IEX": "IEX"}


def parse_sec_company_tickers(text: str) -> list[ListingCandidate]:
    """Parse SEC EDGAR ``company_tickers_exchange.json`` ({"fields":[...],"data":[[cik,name,ticker,exchange]]})
    into US-equity listing candidates. Rows without a ticker or an exchange are skipped (the venue is never
    invented). SEC does not classify ETFs, so every issuer is recorded as STK; refinement is left to the
    NASDAQ ETF flag and IBKR qualification."""
    payload = json.loads(text)
    out: list[ListingCandidate] = []
    fields = [str(f).lower() for f in payload.get("fields", [])]
    rows = payload.get("data", [])
    if fields and rows:
        try:
            i_name, i_tkr, i_exch = fields.index("name"), fields.index("ticker"), fields.index("exchange")
        except ValueError:
            return out
        for row in rows:
            ticker = str(row[i_tkr] or "").strip().upper()
            exch_raw = str(row[i_exch] or "").strip()
            if not ticker or not exch_raw:
                continue                                   # no venue → cannot place it, skip (no fabrication)
            exch = _SEC_EXCHANGE.get(exch_raw.upper(), exch_raw.upper())
            out.append(ListingCandidate(
                symbol=ticker, sec_type="STK", exchange=exch, currency="USD",
                description=str(row[i_name] or "").strip(), source="SEC company_tickers_exchange",
                primary_exchange=exch))
    return out


def read_sec_company_tickers(path: str | Path) -> list[ListingCandidate]:
    return parse_sec_company_tickers(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- venue market plans (documented)
# A small, documented set of major venue facts (ISO-10383 MIC registry: operating country + city → IANA tz).
# Reference data, not fabrication — the same shape as the existing US_MARKET_PLAN. MICs outside this set fall
# back to a generic European plan with country left NULL (NO DATA) rather than a guessed country.
_EU_TZ = "Europe/"
EU_MIC_PLANS: dict[str, MarketPlan] = {
    "XLON": MarketPlan("XLON", "EUROPE", "GB", "Europe/London", "lse", "GBP"),
    "XETR": MarketPlan("XETR", "EUROPE", "DE", "Europe/Berlin", "xetra", "EUR"),
    "XFRA": MarketPlan("XFRA", "EUROPE", "DE", "Europe/Berlin", "xfra", "EUR"),
    "XPAR": MarketPlan("XPAR", "EUROPE", "FR", "Europe/Paris", "euronext", "EUR"),
    "XAMS": MarketPlan("XAMS", "EUROPE", "NL", "Europe/Amsterdam", "euronext", "EUR"),
    "XBRU": MarketPlan("XBRU", "EUROPE", "BE", "Europe/Brussels", "euronext", "EUR"),
    "XLIS": MarketPlan("XLIS", "EUROPE", "PT", "Europe/Lisbon", "euronext", "EUR"),
    "XMIL": MarketPlan("XMIL", "EUROPE", "IT", "Europe/Rome", "borsa_italiana", "EUR"),
    "XMAD": MarketPlan("XMAD", "EUROPE", "ES", "Europe/Madrid", "bme", "EUR"),
    "XSWX": MarketPlan("XSWX", "EUROPE", "CH", "Europe/Zurich", "six", "CHF"),
    "XSTO": MarketPlan("XSTO", "EUROPE", "SE", "Europe/Stockholm", "nasdaq_nordic", "SEK"),
    "XCSE": MarketPlan("XCSE", "EUROPE", "DK", "Europe/Copenhagen", "nasdaq_nordic", "DKK"),
    "XHEL": MarketPlan("XHEL", "EUROPE", "FI", "Europe/Helsinki", "nasdaq_nordic", "EUR"),
    "XOSL": MarketPlan("XOSL", "EUROPE", "NO", "Europe/Oslo", "euronext", "NOK"),
    "XWBO": MarketPlan("XWBO", "EUROPE", "AT", "Europe/Vienna", "wiener_boerse", "EUR"),
    "XDUB": MarketPlan("XDUB", "EUROPE", "IE", "Europe/Dublin", "euronext", "EUR"),
    "XIST": MarketPlan("XIST", "EUROPE", "TR", "Europe/Istanbul", "borsa_istanbul", "TRY"),
}


def firds_market_plan(mic: str) -> MarketPlan:
    """The venue plan for a FIRDS MIC. Known major MICs carry documented country/timezone/calendar; an unknown
    MIC keeps its real MIC as market/exchange but leaves country NULL (NO DATA) rather than inventing one."""
    m = (mic or "").strip().upper()
    known = EU_MIC_PLANS.get(m)
    if known is not None:
        return known
    return MarketPlan(market_id=m or "UNKNOWN", region="EUROPE", country="",
                      timezone="", calendar="eu_generic", default_currency="EUR")


def firds_market_sources(candidates: list[ListingCandidate]) -> list[MarketSource]:
    """Group parsed FIRDS candidates by venue MIC into one `MarketSource` per venue, so the importer isolates
    per-venue failures and resumes per venue. Deterministic order (by MIC)."""
    groups: dict[str, list[ListingCandidate]] = {}
    for cand in candidates:
        groups.setdefault(cand.exchange.strip().upper(), []).append(cand)
    return [MarketSource(plan=firds_market_plan(mic), provider=provider_from_candidates(rows))
            for mic, rows in sorted(groups.items())]


# --------------------------------------------------------------------------- US market plan (SEC + NASDAQ)
US_MARKET_PLAN = MarketPlan(market_id="US", region="AMERICAS", country="US",
                            timezone="America/New_York", calendar="us_equity", default_currency="USD")


def sec_market_source(candidates: list[ListingCandidate]) -> MarketSource:
    return MarketSource(plan=US_MARKET_PLAN, provider=provider_from_candidates(candidates))
