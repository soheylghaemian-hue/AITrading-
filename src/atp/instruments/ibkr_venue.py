"""§ WP10 — IBKR venue & contract resolution: explicit, provenance-tracked MIC → IBKR-exchange registry.

We STRICTLY distinguish four different things that earlier code conflated:

  * **FIRDS MIC** — the ISO 10383 market identifier our reference data carries (e.g. ``XPAR``, ``XETR``).
  * **primary exchange** — the instrument's home listing venue (also a MIC in FIRDS).
  * **trading venue** — where a given line trades (may differ from the primary listing).
  * **IBKR exchange code** — IBKR's OWN, private namespace that ``reqContractDetails`` expects in the
    ``exchange`` field and returns in ``primaryExchange`` (e.g. Euronext Paris = ``SBF``, Xetra = ``IBIS``).

A FIRDS MIC is therefore NOT a valid IBKR exchange code: sending ``exchange=<MIC>`` makes IBKR answer
error 200 ("The destination or exchange selected is Invalid") for cash instruments and
"Invalid value in field # 541" for derivatives — a **query/venue-resolution failure, not a verdict on
tradability**.

Design rules (all fail-closed, never a guess):
  * Discovery is **ISIN-first** (``secIdType='ISIN'``, ``secId=<isin>``, ``exchange='SMART'``) so it does
    NOT depend on this table at all.  ``SMART`` is used only for search/routing.
  * This table is consulted for two things only: (a) **verifying** that a returned ContractDetails'
    IBKR exchange corresponds to the instrument's real listing venue, and (b) supplying a concrete IBKR
    exchange for **derivative** queries that ISIN cannot resolve alone.
  * Mappings are **explicit and hand-curated with a documented provenance** — never derived by string
    munging the MIC.  An unmapped MIC resolves **fail-closed to an empty result**: we can neither assert a
    venue match (so we never VERIFY on venue) nor build a venue-specific derivative query.  A returned
    contract we cannot confirm the venue for, and an unmapped derivative venue, are therefore treated as a
    re-queryable venue-resolution gap (ERROR_RETRYABLE) — never a false terminal NOT_TRADABLE.  (A
    well-formed ISIN query that genuinely returns nothing is still NOT_TRADABLE — that is a real not-found,
    not a venue gap.)

COMPLETENESS CAVEAT (read before relying on this for verification): the authoritative, exhaustive
MIC↔IBKR-exchange correspondence is only obtainable from IBKR's published exchange listing or an entitled
``reqContractDetails`` pass — both out of scope for the local, no-connection WP10 change.  The seed below is
deliberately limited to long-stable, widely-documented correspondences; every other MIC is fail-closed.
Extend it only with an entry that carries a real provenance, never by guessing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VenueMapping:
    """One explicit MIC → IBKR-exchange correspondence with its provenance and curator confidence."""

    mic: str                              # ISO 10383 MIC as it appears in FIRDS (upper-case)
    ibkr_exchanges: tuple[str, ...]       # IBKR exchange code(s) returned as primaryExchange for this venue
    provenance: str                       # where this correspondence is documented / how it was established
    confidence: str                       # 'high' | 'medium' — curator confidence, pending IBKR validation


# --- seed registry -------------------------------------------------------------------------------------
# Only long-stable, widely-documented correspondences. `provenance` names the source; `confidence` is the
# curator's assessment pending validation against IBKR's authoritative exchange listing. NOT exhaustive by
# design — an unlisted MIC is fail-closed (see resolve_ibkr_exchanges).
# Provenance key:
#   REPO = the IBKR exchange code is actively used by this repo's own curated market-data universe
#          (src/atp/marketdata/universe.py GLOBAL_UNIVERSE) — authoritative FOR THIS SYSTEM. High confidence.
#   STD  = IBKR standard, widely-documented exchange code NOT currently cross-referenced elsewhere in this
#          repo — medium confidence, flagged for validation against IBKR's authoritative exchange listing.
# The ISO-10383 MIC for each venue is the standard operating MIC. Never derived from the code — hand-mapped.
_SEED: tuple[VenueMapping, ...] = (
    # --- grounded in src/atp/marketdata/universe.py (REPO provenance) --------------------------------------
    VenueMapping("XNAS", ("NASDAQ",), "REPO: universe.py uses IBKR 'NASDAQ' for US Nasdaq listings", "high"),
    VenueMapping("ARCX", ("ARCA",), "REPO: universe.py uses IBKR 'ARCA' (SPY) for NYSE Arca", "high"),
    VenueMapping("XETR", ("IBIS",), "REPO: universe.py maps Xetra listings to IBKR 'IBIS'", "high"),
    VenueMapping("XLON", ("LSE",), "REPO: universe.py maps London listings to IBKR 'LSE'", "high"),
    VenueMapping("XSWX", ("EBS",), "REPO: universe.py maps SIX Swiss listings to IBKR 'EBS'", "high"),
    VenueMapping("XPAR", ("SBF",), "REPO: universe.py maps Euronext Paris listings to IBKR 'SBF'", "high"),
    VenueMapping("XMIL", ("BVME",), "REPO: universe.py maps Borsa Italiana listings to IBKR 'BVME'", "high"),
    VenueMapping("XSTO", ("SFB",), "REPO: universe.py maps Nasdaq Stockholm listings to IBKR 'SFB'", "high"),
    VenueMapping("XHEL", ("HEX",), "REPO: universe.py maps Nasdaq Helsinki listings to IBKR 'HEX'", "high"),
    VenueMapping("XWBO", ("VSE",), "REPO: universe.py maps Wiener Boerse listings to IBKR 'VSE'", "high"),
    # --- IBKR standard codes not yet cross-referenced in-repo (STD, validate before wide use) --------------
    VenueMapping("XNYS", ("NYSE",), "STD: IBKR standard US NYSE code", "medium"),
    VenueMapping("XASE", ("AMEX",), "STD: IBKR standard NYSE American/AMEX code", "medium"),
    VenueMapping("BATS", ("BATS",), "STD: IBKR standard Cboe BZX/BATS code", "medium"),
    VenueMapping("IEXG", ("IEX",), "STD: IBKR standard IEX code", "medium"),
    VenueMapping("XAMS", ("AEB",), "STD: IBKR standard Euronext Amsterdam code (AEB)", "medium"),
    VenueMapping("XEUR", ("EUREX",), "STD: IBKR standard Eurex derivatives code", "medium"),
    VenueMapping("XCME", ("CME", "GLOBEX"), "STD: IBKR standard CME futures code", "medium"),
    VenueMapping("XCBT", ("CBOT",), "STD: IBKR standard CBOT futures code", "medium"),
    VenueMapping("XNYM", ("NYMEX",), "STD: IBKR standard NYMEX futures code", "medium"),
    VenueMapping("XCEC", ("COMEX",), "STD: IBKR standard COMEX futures code", "medium"),
    VenueMapping("XCBO", ("CBOE",), "STD: IBKR standard Cboe Options Exchange code", "medium"),
)

_BY_MIC: dict[str, VenueMapping] = {m.mic: m for m in _SEED}


def resolve_venue(mic: str | None) -> VenueMapping | None:
    """Return the explicit mapping for a FIRDS MIC, or None (fail-closed) if the MIC is unmapped.

    Never guesses: only an entry present in the curated registry is returned.
    """
    if not mic:
        return None
    return _BY_MIC.get(mic.strip().upper())


def resolve_ibkr_exchanges(mic: str | None) -> tuple[str, ...]:
    """The IBKR exchange code(s) that correspond to a FIRDS MIC, or an empty tuple (fail-closed).

    An empty result means "no authoritative IBKR venue is known for this MIC" — callers must treat that as
    an inability to resolve/verify the venue, never as a licence to fall back to the raw MIC or to SMART.
    """
    m = resolve_venue(mic)
    return m.ibkr_exchanges if m is not None else ()


def is_mapped(mic: str | None) -> bool:
    return resolve_venue(mic) is not None


# The set of IBKR exchange codes this registry knows about — used to recognise a venue token that is ALREADY
# an IBKR exchange code (e.g. US listing sources emit 'NASDAQ'/'ARCA', not MICs) versus a FIRDS ISO MIC.
KNOWN_IBKR_EXCHANGES: frozenset[str] = frozenset(x for m in _SEED for x in m.ibkr_exchanges)


def is_ibkr_exchange(code: str | None) -> bool:
    """True iff `code` is already a known IBKR exchange code (not a FIRDS MIC that needs translating)."""
    return bool(code) and code.strip().upper() in KNOWN_IBKR_EXCHANGES
