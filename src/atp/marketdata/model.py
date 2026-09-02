"""WP4 — the unified, provider-neutral, persistent market-data model.

A single governed shape the whole platform can persist and audit: a quote (bid/ask/last/mid/spread), an
OHLCV bar, or a corporate action — each carrying its full provenance (provider, provider instrument id,
source vs receive timestamp, computed latency, data currency), a fail-closed data-status
(REALTIME/DELAYED/END_OF_DAY/STALE/NO_DATA), an explicit entitlement/license status, a quality status, the
correction/adjustment policy, a corporate-action version, and an immutable provenance checksum.

This module is PURE (no store, no network, no trading). It never fabricates: an absent value stays ``None``
and classification renders NO_DATA. It never labels data REALTIME unless the caller passes BOTH an explicit
positive entitlement AND a verified instrument — a free or unentitled source can never be promoted to
realtime. SAFETY: no order/execution/account path. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum


class DataStatus(str, Enum):
    """The fail-closed market-data status. Distinct from marketdata.quality.QualityStatus (the trading gate)
    — this describes the DATA's realtime/freshness class, never presuming an entitlement."""

    REALTIME = "REALTIME"        # a live tick, entitled AND from a verified instrument, fresh
    DELAYED = "DELAYED"          # explicitly delayed feed (e.g. 15-min), or realtime-claimed but not entitled
    END_OF_DAY = "END_OF_DAY"    # official end-of-day / settlement data
    STALE = "STALE"              # older than the freshness window
    NO_DATA = "NO_DATA"          # no usable value — never fabricated


class EntitlementStatus(str, Enum):
    """Whether the account is entitled to this instrument's data from this provider. Fail-closed default."""

    UNKNOWN = "UNKNOWN"                  # not probed → treated as not entitled
    ENTITLED = "ENTITLED"               # entitled to realtime
    DELAYED_ONLY = "DELAYED_ONLY"       # entitled to delayed only
    NOT_ENTITLED = "NOT_ENTITLED"       # explicitly not entitled


class LicenseType(str, Enum):
    """How the data may be used — recorded explicitly so free data is never treated as more than it is."""

    UNKNOWN = "UNKNOWN"
    FREE_OFFICIAL = "FREE_OFFICIAL"     # an official free source (e.g. exchange EOD) — never auto-realtime/complete
    BROKER_ENTITLED = "BROKER_ENTITLED" # existing broker/provider entitlement already held
    NONE = "NONE"                       # no usage right established


class QualityFlag(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"               # present but suspect (e.g. one-sided book, wide/crossed handled elsewhere)
    INVALID = "INVALID"                 # structurally invalid (negative/crossed/non-numeric)
    NO_DATA = "NO_DATA"


class AdjustmentPolicy(str, Enum):
    RAW = "RAW"                                 # as reported, no adjustment
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"           # split-adjusted only (dividends NOT applied)
    SPLIT_DIV_ADJUSTED = "SPLIT_DIV_ADJUSTED"   # split + dividend adjusted
    UNKNOWN = "UNKNOWN"


class CorporateActionType(str, Enum):
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    DIVIDEND = "DIVIDEND"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    OTHER = "OTHER"


# --------------------------------------------------------------------------- helpers
def to_decimal(value) -> Decimal | None:
    """Parse a finite Decimal, or None. Never raises; a bad value is NO DATA, not a fabricated 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def dec_text(value) -> str | None:
    """Canonical decimal text for checksums/keys (no exponent). None in → None out."""
    d = to_decimal(value)
    if d is None:
        return None
    d = d.normalize()
    if d == 0:
        d = Decimal(0)
    return format(d, "f")


def _positive(value) -> bool:
    d = to_decimal(value)
    return d is not None and d > 0


def mid_price(bid, ask) -> Decimal | None:
    # a mid exists only for a valid two-sided book: both sides POSITIVE and not crossed. A negative/zero or
    # crossed book yields NULL (never a fabricated plausible-looking number).
    b, a = to_decimal(bid), to_decimal(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return (a + b) / Decimal(2)


def spread(bid, ask) -> Decimal | None:
    b, a = to_decimal(bid), to_decimal(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return a - b


def utc_ts(value) -> str | None:
    """Canonical UTC ISO-8601 text (offset +00:00) so timestamps sort lexicographically == chronologically —
    which is what the monotonic out-of-order guard relies on. None/naive/bad → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))   # 3.11+ parses a trailing 'Z' natively
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC).isoformat()


def latency_ms(source_ts, receive_ts) -> Decimal | None:
    """Computed one-way latency = receive - source, in ms. None unless BOTH are aware and receive >= source
    (a negative/backward value is not a valid latency and is dropped rather than fabricated)."""
    s, r = utc_ts(source_ts), utc_ts(receive_ts)
    if s is None or r is None:
        return None
    ds = datetime.fromisoformat(s)
    dr = datetime.fromisoformat(r)
    delta = (dr - ds).total_seconds() * 1000.0
    return None if delta < 0 else to_decimal(f"{delta:.3f}")


def classify_quality(bid, ask, last) -> QualityFlag:
    """Coarse structural quality of a quote's prices. Never fabricates: absent → NO_DATA; a crossed or
    negative price → INVALID; a full two-sided positive book → OK; a one-sided/last-only book → DEGRADED."""
    b, a, la = to_decimal(bid), to_decimal(ask), to_decimal(last)
    for v in (b, a, la):
        if v is not None and v < 0:
            return QualityFlag.INVALID
    if b is not None and a is not None and a < b:
        return QualityFlag.INVALID
    if not any(_positive(v) for v in (b, a, la)):
        return QualityFlag.NO_DATA
    if _positive(b) and _positive(a):
        return QualityFlag.OK
    return QualityFlag.DEGRADED


def classify_bar_quality(open_, high, low, close, volume=None) -> QualityFlag:
    """Structural quality of an OHLCV bar. Never fabricates OK: a negative price/volume, an inverted
    high/low, or a high/low inconsistent with open/close is INVALID; a bar missing any of O/H/L/C is
    DEGRADED; a complete, consistent bar is OK; nothing present is NO_DATA."""
    o, h, l_, c = to_decimal(open_), to_decimal(high), to_decimal(low), to_decimal(close)
    vol = to_decimal(volume)
    prices = (o, h, l_, c)
    if all(v is None for v in prices) and vol is None:
        return QualityFlag.NO_DATA
    if any(v is not None and v < 0 for v in prices) or (vol is not None and vol < 0):
        return QualityFlag.INVALID
    if h is not None and l_ is not None and h < l_:
        return QualityFlag.INVALID
    for v in (o, c):                                     # high/low must bracket open & close when present
        if v is not None:
            if h is not None and h < v:
                return QualityFlag.INVALID
            if l_ is not None and l_ > v:
                return QualityFlag.INVALID
    if all(v is not None for v in prices):
        return QualityFlag.OK
    return QualityFlag.DEGRADED


def classify_data_status(*, declared: DataStatus | str, entitled: bool, verified: bool, has_price: bool,
                         source_ts=None, now=None, max_age_s: float = 30.0) -> DataStatus:
    """Fail-closed data-status classification. The provider DECLARES a class; we only ever DOWNGRADE it:

      * no usable price, or no source timestamp ⇒ NO_DATA (never fabricated);
      * REALTIME requires BOTH an explicit entitlement AND a verified instrument AND freshness — otherwise a
        realtime claim is downgraded to DELAYED (entitlement is a fact WP3 VERIFIED never implies);
      * anything older than the freshness window ⇒ STALE; a future-dated source ⇒ NO_DATA;
      * END_OF_DAY and DELAYED pass through (they never claim realtime).
    """
    want = DataStatus(declared) if not isinstance(declared, DataStatus) else declared
    if not has_price:
        return DataStatus.NO_DATA
    s = utc_ts(source_ts)
    if s is None:
        return DataStatus.NO_DATA
    if want is DataStatus.END_OF_DAY:
        return DataStatus.END_OF_DAY
    # freshness
    now_ts = utc_ts(now) or utc_ts(datetime.now(UTC))
    age = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(s)).total_seconds()
    if age < 0:
        return DataStatus.NO_DATA                     # future-dated → not evidence, never realtime
    if want is DataStatus.REALTIME:
        if not (entitled and verified):
            return DataStatus.DELAYED                 # fail-closed: never realtime without entitlement+verified
        if age > max_age_s:
            return DataStatus.STALE
        return DataStatus.REALTIME
    if want is DataStatus.DELAYED:
        return DataStatus.STALE if age > max_age_s else DataStatus.DELAYED
    return DataStatus.NO_DATA


# --------------------------------------------------------------------------- records
@dataclass(frozen=True, slots=True)
class QuoteObservation:
    """One provider-neutral quote at a point in time — the unified persisted quote shape."""

    instrument_id: str
    provider: str
    provider_instrument_id: str | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    volume: Decimal | None = None
    reference_price: Decimal | None = None
    previous_close: Decimal | None = None
    data_currency: str | None = None
    source_ts: str | None = None
    receive_ts: str | None = None
    data_status: str = DataStatus.NO_DATA.value
    entitlement_status: str = EntitlementStatus.UNKNOWN.value
    license: str = LicenseType.UNKNOWN.value
    quality_status: str = QualityFlag.NO_DATA.value
    adjustment_policy: str = AdjustmentPolicy.RAW.value
    corporate_action_version: int = 0

    @property
    def mid(self) -> Decimal | None:
        return mid_price(self.bid, self.ask)

    @property
    def spread(self) -> Decimal | None:
        return spread(self.bid, self.ask)

    @property
    def latency_ms(self) -> Decimal | None:
        return latency_ms(self.source_ts, self.receive_ts)

    @property
    def checksum(self) -> str:
        return _checksum("quote.v1", {
            "instrument_id": self.instrument_id, "provider": self.provider,
            "provider_instrument_id": self.provider_instrument_id,
            "bid": dec_text(self.bid), "ask": dec_text(self.ask), "last": dec_text(self.last),
            "bid_size": dec_text(self.bid_size), "ask_size": dec_text(self.ask_size),
            "volume": dec_text(self.volume), "reference_price": dec_text(self.reference_price),
            "previous_close": dec_text(self.previous_close), "data_currency": self.data_currency,
            "source_ts": utc_ts(self.source_ts), "data_status": self.data_status,
            "entitlement_status": self.entitlement_status, "license": self.license,
            "quality_status": self.quality_status, "adjustment_policy": self.adjustment_policy,
            "corporate_action_version": int(self.corporate_action_version),
        })


@dataclass(frozen=True, slots=True)
class BarObservation:
    instrument_id: str
    provider: str
    provider_instrument_id: str | None
    interval: str
    ts: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    trade_count: int | None = None
    data_currency: str | None = None
    source_ts: str | None = None
    receive_ts: str | None = None
    data_status: str = DataStatus.NO_DATA.value
    entitlement_status: str = EntitlementStatus.UNKNOWN.value
    license: str = LicenseType.UNKNOWN.value
    quality_status: str = QualityFlag.NO_DATA.value
    adjustment_policy: str = AdjustmentPolicy.RAW.value
    corporate_action_version: int = 0

    @property
    def checksum(self) -> str:
        return _checksum("bar.v1", {
            "instrument_id": self.instrument_id, "provider": self.provider, "interval": self.interval,
            "ts": utc_ts(self.ts), "open": dec_text(self.open), "high": dec_text(self.high),
            "low": dec_text(self.low), "close": dec_text(self.close), "volume": dec_text(self.volume),
            "trade_count": self.trade_count, "data_currency": self.data_currency,
            "data_status": self.data_status, "adjustment_policy": self.adjustment_policy,
            "corporate_action_version": int(self.corporate_action_version),
        })


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: str
    provider: str
    action_type: str
    effective_date: str
    corporate_action_version: int = 0
    ex_date: str | None = None
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None

    @property
    def checksum(self) -> str:
        return _checksum("ca.v1", {
            "instrument_id": self.instrument_id, "provider": self.provider, "action_type": self.action_type,
            "effective_date": self.effective_date, "corporate_action_version": int(self.corporate_action_version),
            "ex_date": self.ex_date, "ratio": dec_text(self.ratio), "cash_amount": dec_text(self.cash_amount),
            "currency": self.currency,
        })


@dataclass(frozen=True, slots=True)
class ProviderEntitlement:
    """An instrument's mapping to a provider plus the explicitly-recorded availability + license/entitlement.
    Fail-closed defaults; free data is stored as FREE_OFFICIAL and never treated as realtime."""

    instrument_id: str
    provider: str
    provider_instrument_id: str | None = None
    entitlement_status: str = EntitlementStatus.UNKNOWN.value
    license: str = LicenseType.UNKNOWN.value
    realtime_available: bool = False
    available: bool = False
    capabilities_json: str = "{}"
    rate_limit_json: str = "{}"


def _checksum(tag: str, payload: dict) -> str:
    payload = {**payload, "tag": tag}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
