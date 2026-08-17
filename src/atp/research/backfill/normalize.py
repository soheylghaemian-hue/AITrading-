"""§ R3.0A — RTH normalization: split-adjusted 1-minute aggregates → regular-session daily bars.

Correction #3: the provider's Custom daily aggregate mixes pre/regular/after-hours, so R3.0A builds the
research daily bar from 1-MINUTE aggregates filtered to the versioned NYSE regular session:

  * timezone America/New_York, regular session 09:30–16:00 (early close 09:30–13:00), DST-aware.
  * a minute bar labelled t (its window start) is in RTH iff session_open ≤ t < session_close, so the
    09:30 opening minute is INCLUDED and the 16:00 boundary minute is EXCLUDED (last minute is 15:59).
  * per completed session: open = first RTH minute's open, high = max RTH high, low = min RTH low,
    close = last RTH minute's close, volume = Σ adjusted RTH minute volume (Decimal, never rounded),
    trade_count = Σ minute counts only when every minute reports one, else NULL.
  * pre-market / after-hours minutes are excluded; the incomplete current session is excluded; a session
    with < MISSING_MINUTE_THRESHOLD of its expected RTH minutes (or a missing 09:30 open) is rejected.

Adjustment: MASSIVE_SPLIT_ADJUSTED_RTH_V1 — provider `adjusted=true` (splits, NOT dividends). Performance
built on these bars is PRICE RETURN, not total shareholder return. Adjusted volume stays Decimal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .. import calendars as cal

PROVIDER = "MASSIVE"
# R3.0A.1: bumped v1 → chunked-v2. The provider request contract changed (bounded, session-aligned date
# chunks with per-chunk page/result caps instead of one multi-year request), so the canonical request
# checksum changes with it — a v1 dataset is NEVER silently reused as if produced by the new contract.
PROVIDER_CONTRACT_VERSION = "polygon-aggs-1min-chunked-v2"
ADJUSTMENT_POLICY = "MASSIVE_SPLIT_ADJUSTED_RTH_V1"
NORMALIZATION_POLICY = "US_EQUITY_RTH_DAILY_FROM_1MIN_V1"
# A completed session must retain at least this fraction of its expected RTH minutes (and the 09:30 open),
# else it is rejected. Liquid US large-caps print essentially every RTH minute; < 90% signals a halt/gap,
# so a daily bar built from that sparse minute set would misstate OHLCV and is excluded (never fabricated).
MISSING_MINUTE_THRESHOLD = Decimal("0.90")


@dataclass(frozen=True, slots=True)
class MinuteBar:
    ts: datetime                 # aware UTC, window start
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None = None


def expected_rth_minutes(d: date) -> int:
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    return int((c - o).total_seconds() // 60)


def _minute_in_rth(minute_ts: datetime, d: date) -> bool:
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    return o <= minute_ts < c


def normalize_minutes_to_daily(symbol: str, minutes: list[MinuteBar], start: datetime, end: datetime,
                               *, now: datetime | None = None,
                               threshold: Decimal = MISSING_MINUTE_THRESHOLD) -> dict:
    """Returns {bars, missing_sessions, out_of_session_minutes, warnings}. Deterministic + pure."""
    now = now or datetime.now(timezone.utc)
    # bucket RTH minutes by their NY session date
    by_session: dict[str, list[MinuteBar]] = {}
    out_of_session = 0
    for mb in minutes:
        d = mb.ts.astimezone(cal.NY).date()
        if cal.is_session_day(d) and _minute_in_rth(mb.ts, d):
            by_session.setdefault(d.isoformat(), []).append(mb)
        else:
            out_of_session += 1

    bars: list[dict] = []
    missing: list[dict] = []
    warnings: list[str] = []
    d = start.astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc).date()
    while d <= last:
        if cal.is_session_day(d):
            # exclude the incomplete current session (its close is not yet in the past)
            if cal.session_close_utc(d) > now:
                missing.append({"session_date": d.isoformat(), "reason": "INCOMPLETE_CURRENT_SESSION"})
                d = _next_day(d)
                continue
            mins = sorted(by_session.get(d.isoformat(), []), key=lambda m: m.ts)
            exp = expected_rth_minutes(d)
            # The EXACT expected opening and closing RTH minutes: 09:30 open, and the last in-session minute
            # (session close − 1min = 15:59 on a normal session, 12:59 on an early close). Both must be
            # present — a close is NEVER manufactured from an earlier minute (correction #4/R3.0A.1).
            has_open = bool(mins) and mins[0].ts == cal.session_open_utc(d)
            has_close = bool(mins) and mins[-1].ts == cal.session_close_utc(d) - timedelta(minutes=1)
            if not mins or Decimal(len(mins)) < threshold * Decimal(exp) or not has_open or not has_close:
                missing.append({"session_date": d.isoformat(), "reason": "INSUFFICIENT_SESSION_MINUTES",
                                "available": len(mins), "expected": exp,
                                "open_minute_present": has_open, "close_minute_present": has_close})
            else:
                counts = [m.trade_count for m in mins]
                trade_count = sum(counts) if all(c is not None for c in counts) else None  # type: ignore[arg-type]
                bars.append({
                    "symbol": symbol, "interval": "1D",
                    "ts": datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                    "session_date": d.isoformat(),
                    "open": mins[0].open, "high": max(m.high for m in mins),
                    "low": min(m.low for m in mins), "close": mins[-1].close,
                    "volume": sum((m.volume for m in mins), Decimal(0)), "trade_count": trade_count,
                    "source": PROVIDER, "adjustment_policy": ADJUSTMENT_POLICY})
        d = _next_day(d)
    return {"bars": bars, "missing_sessions": missing, "out_of_session_minutes": out_of_session,
            "warnings": warnings}


def _next_day(d: date) -> date:
    return d + timedelta(days=1)


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent NYSE session whose regular close is strictly in the past (never the in-progress day)."""
    now = now or datetime.now(timezone.utc)
    d = now.astimezone(cal.NY).date()
    while True:
        if cal.is_session_day(d) and cal.session_close_utc(d) <= now:
            return d
        d = d - timedelta(days=1)
