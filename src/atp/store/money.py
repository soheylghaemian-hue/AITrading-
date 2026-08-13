"""Exact monetary values (§ Phase B). Money is NEVER binary floating point as authoritative state.

Authoritative money is `decimal.Decimal`, stored as NUMERIC in PostgreSQL and as a canonical decimal
string (TEXT) in SQLite. This module is the single conversion point so both backends round-trip the
same exact value.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# 8 dp covers equities, FX pips and share/ível sizes without binary error.
QUANT = Decimal("0.00000001")


def D(value) -> Decimal:
    """Coerce to Decimal safely. Floats are routed through str() so 0.1 stays 0.1, never 0.1000000…"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("money value is None")
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a valid money value: {value!r}") from exc


def money_str(value) -> str:
    """Canonical, exact string for storage (SQLite TEXT / display). Quantized to 8 dp, half-even."""
    return str(D(value).quantize(QUANT, rounding=ROUND_HALF_EVEN))


def opt_money_str(value) -> str | None:
    return None if value is None else money_str(value)


def to_decimal(value) -> Decimal | None:
    """Read a stored money value (str/Decimal/None) back into an exact Decimal."""
    return None if value is None else D(value)
