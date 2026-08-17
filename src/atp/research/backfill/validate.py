"""§ R3.0A — structural validation of provider minutes and normalized daily bars, plus the two checksums.

`validate_minutes` guards the raw provider input (aware-UTC, strictly ascending + unique timestamps,
OHLC invariants low ≤ open/close ≤ high with high ≥ low, non-negative volume, non-negative trade_count).
`validate_daily_bars` guards the normalized output (each bar's timestamp is an expected session bucket for
its symbol, strictly ascending per symbol, OHLC invariants, non-negative Decimal volume). `raw_pages_checksum`
fingerprints the exact provider pages consumed and `dataset_checksum` fingerprints the normalized bars — two
independent checksums (correction #6) so a change in either provider bytes OR normalization is detectable.
Pure; no I/O; no order/broker/execution path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timezone
from decimal import Decimal

from ...store.money import money_str
from .. import calendars as cal
from .normalize import MinuteBar


class ValidationError(Exception):
    code = "DATASET_VALIDATION_FAILED"


def _ohlc_ok(o: Decimal, h: Decimal, l: Decimal, c: Decimal) -> bool:
    return h >= l and l <= o <= h and l <= c <= h


def validate_minutes(symbol: str, minutes: list[MinuteBar]) -> None:
    prev = None
    for i, m in enumerate(minutes):
        if m.ts.tzinfo is None or m.ts.utcoffset() != timezone.utc.utcoffset(None):
            raise ValidationError(f"{symbol} minute #{i} timestamp is not aware-UTC: {m.ts!r}")
        if prev is not None and m.ts <= prev:
            raise ValidationError(f"{symbol} minutes not strictly ascending/unique at #{i}: {prev} -> {m.ts}")
        if not _ohlc_ok(m.open, m.high, m.low, m.close):
            raise ValidationError(f"{symbol} minute #{i} violates OHLC invariants: "
                                  f"o={m.open} h={m.high} l={m.low} c={m.close}")
        if m.volume < 0:
            raise ValidationError(f"{symbol} minute #{i} negative volume {m.volume}")
        if m.trade_count is not None and m.trade_count < 0:
            raise ValidationError(f"{symbol} minute #{i} negative trade_count {m.trade_count}")
        prev = m.ts


def validate_daily_bars(bars: list[dict]) -> None:
    policy = cal.resolve_policy("SPY", "1D")   # 1D availability policy (symbol-independent for 1D buckets)
    last_ts_by_symbol: dict[str, str] = {}
    for i, b in enumerate(bars):
        sym = b["symbol"]
        ts = cal.norm_ts(b["ts"])
        d = cal.parse_ts(b["ts"]).date()
        if not cal.is_session_day(d):
            raise ValidationError(f"{sym} daily bar #{i} ts {ts} is not a session day")
        expected = cal.expected_bar_timestamps(cal.parse_ts(b["ts"]), cal.parse_ts(b["ts"]), policy)
        if ts not in expected:
            raise ValidationError(f"{sym} daily bar #{i} ts {ts} is not the expected session bucket")
        prev = last_ts_by_symbol.get(sym)
        if prev is not None and ts <= prev:
            raise ValidationError(f"{sym} daily bars not strictly ascending at #{i}: {prev} -> {ts}")
        o, h, l, c = (Decimal(str(b["open"])), Decimal(str(b["high"])),
                      Decimal(str(b["low"])), Decimal(str(b["close"])))
        if not _ohlc_ok(o, h, l, c):
            raise ValidationError(f"{sym} daily bar #{i} violates OHLC invariants: o={o} h={h} l={l} c={c}")
        if Decimal(str(b["volume"])) < 0:
            raise ValidationError(f"{sym} daily bar #{i} negative volume {b['volume']}")
        last_ts_by_symbol[sym] = ts


def raw_pages_checksum(pages_by_symbol: dict[str, list[dict]]) -> str:
    """Deterministic fingerprint of the exact provider pages consumed (symbol-ordered, page-ordered)."""
    h = hashlib.sha256()
    for sym in sorted(pages_by_symbol):
        for page in pages_by_symbol[sym]:
            h.update(json.dumps({"symbol": sym, "adjusted": page.get("adjusted"),
                                 "results": page.get("results", [])},
                                sort_keys=True, separators=(",", ":"), default=str).encode())
    return "sha256:" + h.hexdigest()


def dataset_checksum(bars: list[dict]) -> str:
    """Deterministic fingerprint of the normalized daily bars. Money is canonicalized with the store's
    `money_str` (8 dp, half-even) so the checksum is byte-identical whether computed from freshly
    normalized Decimals OR re-read from the store's canonical TEXT — which is what makes the on-read
    checksum RE-VERIFICATION in `select.validate_selection` sound."""
    h = hashlib.sha256()
    for b in sorted(bars, key=lambda x: (x["symbol"], cal.norm_ts(x["ts"]))):
        h.update(json.dumps({
            "symbol": b["symbol"], "interval": b["interval"], "ts": cal.norm_ts(b["ts"]),
            "session_date": b["session_date"], "open": money_str(b["open"]), "high": money_str(b["high"]),
            "low": money_str(b["low"]), "close": money_str(b["close"]), "volume": money_str(b["volume"]),
            "trade_count": b["trade_count"], "adjustment_policy": b["adjustment_policy"],
        }, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + h.hexdigest()
