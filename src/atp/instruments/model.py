"""Unified, persistent global-instrument reference model (WP2 — Globales Markt- und Instrumentenmodell).

This is the *additive foundation* for a world-wide instrument catalogue. It defines a single,
broker-neutral reference record (`InstrumentRecord`) that carries everything the rest of the platform
needs to identify an instrument unambiguously across markets:

  * a **stable, provider-neutral internal id** derived deterministically from the instrument's venue
    identity (so re-importing the same contract is idempotent and yields the SAME id);
  * cross-vendor identifiers — IBKR ``con_id``, ``isin``, ``figi``, ``cusip``, ``sedol`` and the venue
    ``local_symbol`` — each **optional**: an absent identifier is stored as ``NULL`` (NO DATA), it is
    NEVER invented;
  * venue / classification — region, country, exchange, primary exchange, trading & settlement currency,
    IANA timezone, trading calendar;
  * asset class / sub-class / underlying and contract terms (tick size, multiplier, expiry, strike, right);
  * governance status — tradability / market-data / source / verification — all defaulting to the most
    conservative value so nothing is presumed tradeable or entitled.

**Symbol-collision protection.** The natural key includes the *exchange* and *currency*, so the same
ticker on two venues (e.g. ``AAPL`` on NASDAQ vs. a foreign line) yields two distinct instruments with
two distinct ids. The internal id is a hash of that natural key, so a collision is structurally impossible
and the persistence layer additionally enforces ``UNIQUE(natural_key)``.

SAFETY: this module is pure reference data. It performs no trading, no order/execution/broker call, no
market-data subscription and no IBKR qualification. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..core.enums import AssetClass


class VerificationStatus(str, Enum):
    """How thoroughly the record's identity has been proven. Listing discovery only reaches UNVERIFIED;
    broker qualification (out of WP2 scope) is what later promotes a record to VERIFIED."""

    UNVERIFIED = "unverified"      # discovered from a reference/listing source, not yet broker-qualified
    VERIFIED = "verified"          # identity proven against an authoritative source (future work)
    REJECTED = "rejected"          # failed validation


class TradabilityStatus(str, Enum):
    """Fail-closed: nothing is assumed tradeable until proven."""

    UNKNOWN = "unknown"
    TRADABLE = "tradable"
    NOT_TRADABLE = "not_tradable"


class MarketDataStatus(str, Enum):
    """Fail-closed: no market-data entitlement is assumed."""

    UNKNOWN = "unknown"
    ENTITLED = "entitled"
    DELAYED = "delayed"
    NONE = "none"


class SourceStatus(str, Enum):
    """Provenance of the record within the import lifecycle."""

    DISCOVERED = "discovered"      # seen in a reference/listing source
    CONFIRMED = "confirmed"        # corroborated by a second authoritative source (future work)
    STALE = "stale"                # no longer present in the latest source snapshot


# Broker/reference security-type → domain asset class. Mirrors the catalogue's mapping so the persistent
# model and the in-memory catalogue agree on classification.
_SEC_TYPE: dict[str, AssetClass] = {
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

_SUBCLASS: dict[str, str] = {
    "STK": "common_stock",
    "ETF": "exchange_traded_fund",
    "IND": "index",
    "FUND": "fund",
}

# Asset classes whose contract multiplier is definitionally 1 (cash instruments). For these we may store
# "1" without inventing data; for derivatives the multiplier is genuinely unknown at listing stage → NULL.
_UNIT_MULTIPLIER = frozenset(
    {AssetClass.EQUITY, AssetClass.ETF, AssetClass.INDEX, AssetClass.FUND, AssetClass.FX}
)


def sec_type_to_asset_class(sec_type: str | None) -> AssetClass | None:
    return _SEC_TYPE.get((sec_type or "").strip().upper())


def _canon_str(value: str | None) -> str:
    return (value or "").strip()


def canon_decimal_text(value) -> str | None:
    """Canonical, dialect-agnostic decimal text (no exponent, trailing zeros trimmed). ``None`` in → ``None``
    out. Used so the same numeric value always serializes identically (stable natural keys & checksums)."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    d = d.normalize()
    if d == 0:
        d = Decimal(0)
    return format(d, "f")


def instrument_natural_key(
    *,
    asset_class: AssetClass | str,
    symbol: str,
    exchange: str,
    currency: str,
    expiry: str | None = None,
    strike=None,
    option_right: str | None = None,
) -> str:
    """The venue-anchored, broker-neutral contract identity. Deterministic canonical JSON. Because the key
    includes ``exchange`` and ``currency``, the same ticker on two venues produces two distinct keys — this
    is the symbol-collision protection across exchanges."""
    ac = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()
    payload = {
        "asset_class": ac,
        "symbol": _canon_str(symbol).upper(),
        "exchange": _canon_str(exchange).upper(),
        "currency": _canon_str(currency).upper(),
        "expiry": _canon_str(expiry),
        "strike": canon_decimal_text(strike) or "",
        "right": _canon_str(option_right).upper(),
        "tag": "atp.instrument.natural-key.v1",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def instrument_id_for(natural_key: str) -> str:
    """Stable, provider-neutral surrogate id derived from the natural key. Same contract → same id."""
    return "INS-" + hashlib.sha256(natural_key.encode("utf-8")).hexdigest()[:24]


# The mutable (non-identity) descriptive columns, in persistence order. Shared by the record and the store
# so the two never drift. ``content_checksum`` is derived from these and appended by the store.
CONTENT_FIELDS: tuple[str, ...] = (
    "con_id", "isin", "figi", "cusip", "sedol", "local_symbol", "symbol", "description",
    "region", "country", "exchange", "primary_exchange", "trading_currency", "settlement_currency",
    "timezone", "trading_calendar", "calendar_version", "asset_class", "sub_class",
    "underlying_symbol", "underlying_instrument_id", "tick_size", "multiplier", "lot_size", "min_size",
    "expiry", "strike", "option_right", "tradability_status", "market_data_status",
    "source_status", "verification_status", "source", "last_verified_at",
)


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """One unified, broker-neutral instrument. Identity fields (``asset_class``/``symbol``/``exchange``/
    ``trading_currency`` and, for derivatives, ``expiry``/``strike``/``option_right``) determine the natural
    key and the stable id; everything else is descriptive. Unknown identifiers stay ``None`` — never faked."""

    # --- identity inputs ---------------------------------------------------
    symbol: str
    asset_class: AssetClass
    exchange: str
    trading_currency: str = "USD"

    # --- cross-vendor identifiers (None = NO DATA) -------------------------
    con_id: int | None = None
    isin: str | None = None
    figi: str | None = None
    cusip: str | None = None
    sedol: str | None = None
    local_symbol: str | None = None
    description: str | None = None

    # --- venue / classification -------------------------------------------
    region: str | None = None
    country: str | None = None
    primary_exchange: str | None = None
    settlement_currency: str | None = None
    timezone: str | None = None
    trading_calendar: str | None = None
    calendar_version: str | None = None
    sub_class: str | None = None
    underlying_symbol: str | None = None
    underlying_instrument_id: str | None = None

    # --- contract terms (canonical decimal text; None = unknown) ----------
    tick_size: str | None = None
    multiplier: str | None = None
    lot_size: str | None = None
    min_size: str | None = None
    expiry: str | None = None
    strike: str | None = None
    option_right: str | None = None

    # --- governance / status (fail-closed defaults) -----------------------
    tradability_status: str = TradabilityStatus.UNKNOWN.value
    market_data_status: str = MarketDataStatus.UNKNOWN.value
    source_status: str = SourceStatus.DISCOVERED.value
    verification_status: str = VerificationStatus.UNVERIFIED.value
    source: str | None = None
    last_verified_at: str | None = None

    @property
    def natural_key(self) -> str:
        return instrument_natural_key(
            asset_class=self.asset_class, symbol=self.symbol, exchange=self.exchange,
            currency=self.trading_currency, expiry=self.expiry, strike=self.strike,
            option_right=self.option_right,
        )

    @property
    def instrument_id(self) -> str:
        return instrument_id_for(self.natural_key)

    def _content(self) -> dict:
        ac = self.asset_class.value if isinstance(self.asset_class, AssetClass) else str(self.asset_class)
        raw = {
            "con_id": None if self.con_id is None else int(self.con_id),
            "isin": self.isin, "figi": self.figi, "cusip": self.cusip, "sedol": self.sedol,
            "local_symbol": self.local_symbol, "symbol": self.symbol, "description": self.description,
            "region": self.region, "country": self.country, "exchange": self.exchange,
            "primary_exchange": self.primary_exchange, "trading_currency": self.trading_currency,
            "settlement_currency": self.settlement_currency, "timezone": self.timezone,
            "trading_calendar": self.trading_calendar, "calendar_version": self.calendar_version,
            "asset_class": ac, "sub_class": self.sub_class, "underlying_symbol": self.underlying_symbol,
            "underlying_instrument_id": self.underlying_instrument_id,
            "tick_size": canon_decimal_text(self.tick_size), "multiplier": canon_decimal_text(self.multiplier),
            "lot_size": canon_decimal_text(self.lot_size), "min_size": canon_decimal_text(self.min_size),
            "expiry": self.expiry, "strike": canon_decimal_text(self.strike), "option_right": self.option_right,
            "tradability_status": self.tradability_status, "market_data_status": self.market_data_status,
            "source_status": self.source_status, "verification_status": self.verification_status,
            "source": self.source, "last_verified_at": self.last_verified_at,
        }
        return raw

    @property
    def content_checksum(self) -> str:
        canonical = json.dumps(self._content(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_record(self) -> dict:
        """Flat mapping of every persisted column value (identity + content + checksum)."""
        rec = dict(self._content())
        rec["instrument_id"] = self.instrument_id
        rec["natural_key"] = self.natural_key
        rec["content_checksum"] = self.content_checksum
        return rec
