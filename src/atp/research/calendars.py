"""§ R3.0 — asset class + point-in-time bar availability (RESEARCH ONLY, deterministic, pure stdlib).

Correction #1: equities are NOT a continuous UTC market. R3.0 models `US_EQUITY_SESSION_CLOSE_V1`:
America/New_York, regular session 09:30–16:00 ET, DST-correct via `zoneinfo`, with an explicit,
versioned NYSE holiday + early-close table. A bar may be used by a decision at time T only if its
market-availability time (NOT its DB ingest time) is ≤ T:

  * `1D` — the daily bar for a UTC-calendar date maps to that date's US regular session; the US session
    (09:30–16:00 ET ≈ 13:30–21:00 UTC) always falls inside the same UTC calendar day, so a UTC-day bar
    for date D becomes available at D's real session close (16:00 ET, or 13:00 ET on early-close days).
    This is the actual session close — NOT a naïve "+24h".
  * `1h` — a bar becomes available at the end of its UTC hour bucket (`ts + 1h`). Only hour buckets that
    overlap the session are ever expected; overnight/weekend/holiday hours are not "missing" bars.

Ingest time (`ohlc_bars.created_at`) is deliberately ignored for availability, so a late historical
backfill neither grants future visibility nor becomes unusable. If a symbol's asset class / calendar
cannot be determined safely, `resolve_policy` raises `PointInTimeError` (INSUFFICIENT_POINT_IN_TIME_METADATA)
— it never guesses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
EXCHANGE_TZ = "America/New_York"
CALENDAR_ID = "NYSE"
CALENDAR_VERSION = "NYSE_2023_2027_V1"
POLICY_ID = "US_EQUITY_SESSION_CLOSE_V1"
POLICY_VERSION = 1
SESSION_CALENDAR = "US_EQUITY_RTH"
ASSET_CLASS = "US_EQUITY"
SUPPORTED_INTERVALS = ("1h", "1D")
# Declared authoritative coverage of the versioned NYSE calendar table below. A request touching any
# date outside this window cannot be evaluated safely (unknown holidays) → INSUFFICIENT_POINT_IN_TIME_METADATA.
CALENDAR_START = date(2023, 1, 1)
CALENDAR_END = date(2027, 12, 31)


class PointInTimeError(Exception):
    """A symbol/interval cannot be mapped to a safe point-in-time availability policy."""

    code = "INSUFFICIENT_POINT_IN_TIME_METADATA"


# NYSE full-closure holidays (observed dates), 2023–2027. Version-pinned; extend as a data change.
_HOLIDAYS = frozenset({
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04",
    "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
    "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
})
# Early-close sessions (regular close 13:00 ET instead of 16:00 ET).
_EARLY_CLOSE = frozenset({
    "2023-07-03", "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28",
    "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26",
})
# Symbols whose asset class + calendar are KNOWN to be US equities/ETFs on the NYSE/Nasdaq RTH session.
US_EQUITY_SYMBOLS = frozenset({
    "NVDA", "AAPL", "MSFT", "SPY", "QQQ", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AMD", "NFLX",
    "JPM", "V", "XLF", "IWM", "DIA", "INTC", "MU", "AVGO",
})


@dataclass(frozen=True, slots=True)
class AvailabilityPolicy:
    policy_id: str
    policy_version: int
    calendar_id: str
    calendar_version: str
    exchange_tz: str
    session_calendar: str
    asset_class: str
    interval: str


def normalize_interval(interval: str) -> str:
    iv = (interval or "").strip()
    low = iv.lower()
    if low == "1d":
        return "1D"
    if low == "1h":
        return "1h"
    return iv


def resolve_policy(symbol: str, interval: str) -> AvailabilityPolicy:
    """Map (symbol, interval) → the safe availability policy, or raise PointInTimeError. Never guesses."""
    iv = normalize_interval(interval)
    sym = (symbol or "").upper()
    if sym not in US_EQUITY_SYMBOLS:
        raise PointInTimeError(f"asset class / exchange calendar unknown for symbol '{sym}' — cannot "
                               f"establish point-in-time availability safely")
    if iv not in SUPPORTED_INTERVALS:
        raise PointInTimeError(f"unsupported interval '{interval}' (R3.0 supports 1h and 1D for US equities)")
    return AvailabilityPolicy(POLICY_ID, POLICY_VERSION, CALENDAR_ID, CALENDAR_VERSION, EXCHANGE_TZ,
                              SESSION_CALENDAR, ASSET_CLASS, iv)


def is_session_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _HOLIDAYS


def is_early_close(d: date) -> bool:
    return d.isoformat() in _EARLY_CLOSE


def session_open_utc(d: date) -> datetime:
    return datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(timezone.utc)


def session_close_utc(d: date) -> datetime:
    close_t = time(13, 0) if is_early_close(d) else time(16, 0)
    return datetime.combine(d, close_t, tzinfo=NY).astimezone(timezone.utc)


def parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime (assumes UTC when naïve)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def available_at(bar_ts: str, policy: AvailabilityPolicy) -> datetime:
    """The earliest wall-clock time at which `bar_ts` (event-time bar open) is safely known."""
    dt = parse_ts(bar_ts)
    if policy.interval == "1D":
        return session_close_utc(dt.date())
    return dt + timedelta(hours=1)


def _expected_1h_buckets(d: date) -> int:
    """Number of UTC hour buckets that overlap date d's US session (DST + early-close aware)."""
    o, c = session_open_utc(d), session_close_utc(d)
    b = o.replace(minute=0, second=0, microsecond=0)
    n = 0
    while b < c:
        n += 1
        b += timedelta(hours=1)
    return n


def expected_bars(start: datetime, end: datetime, policy: AvailabilityPolicy) -> int:
    """Expected bar count over [start, end] from the SESSION calendar (not continuous clock time)."""
    total, d, last = 0, start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()
    while d <= last:
        if is_session_day(d):
            total += 1 if policy.interval == "1D" else _expected_1h_buckets(d)
        d += timedelta(days=1)
    return total


def next_expected_bar(prev_ts: str, policy: AvailabilityPolicy) -> datetime:
    """The event-time of the bar that should immediately follow `prev_ts` per the SESSION calendar —
    the next in-session bucket, or (across an overnight/weekend/holiday/early-close boundary) the first
    bucket of the next session. This is what makes a session boundary NOT a gap, while a genuinely
    missing in-session bar IS a gap."""
    prev = parse_ts(prev_ts)
    if policy.interval == "1D":
        d = prev.date() + timedelta(days=1)
        while not is_session_day(d):
            d += timedelta(days=1)
        return datetime.combine(d, time(0, 0), tzinfo=timezone.utc)
    nxt = prev + timedelta(hours=1)
    if nxt < session_close_utc(prev.date()):
        return nxt
    nd = prev.date() + timedelta(days=1)
    while not is_session_day(nd):
        nd += timedelta(days=1)
    return session_open_utc(nd).replace(minute=0, second=0, microsecond=0)


def is_contiguous(prev_ts: str, next_ts: str, policy: AvailabilityPolicy) -> bool:
    """True if `next_ts` is the calendar-expected successor of `prev_ts` (a session boundary counts as
    contiguous; a skipped in-session bar does not)."""
    return parse_ts(next_ts) == next_expected_bar(prev_ts, policy)


def calendar_covers(start: datetime, end: datetime) -> bool:
    """Whether [start, end] lies entirely inside the versioned calendar's declared coverage."""
    return (CALENDAR_START <= start.astimezone(timezone.utc).date()
            and end.astimezone(timezone.utc).date() <= CALENDAR_END)


def bar_open_utc(bar_ts: str, policy: AvailabilityPolicy) -> datetime:
    """The LOGICAL open time of a bar (when a next-bar-open fill actually executes). For 1D this is the
    session open (09:30 ET → UTC), NOT the UTC-midnight bucket label; for 1h it is the bucket start."""
    dt = parse_ts(bar_ts)
    return session_open_utc(dt.date()) if policy.interval == "1D" else dt


def norm_ts(ts: str) -> str:
    """Canonical UTC ISO key for timestamp-set membership (dialect/format-agnostic)."""
    return parse_ts(ts).isoformat()


def _session_hour_buckets(d: date) -> list[datetime]:
    o, c = session_open_utc(d), session_close_utc(d)
    b, out = o.replace(minute=0, second=0, microsecond=0), []
    while b < c:
        out.append(b)
        b += timedelta(hours=1)
    return out


def expected_bar_timestamps(start: datetime, end: datetime, policy: AvailabilityPolicy) -> set[str]:
    """The EXACT set of expected bar-open timestamps (canonical UTC ISO) over [start, end] from the
    SESSION calendar — used for membership-based coverage (not just counts)."""
    out: set[str] = set()
    d, last = start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()
    while d <= last:
        if is_session_day(d):
            if policy.interval == "1D":
                out.add(datetime.combine(d, time(0, 0), tzinfo=timezone.utc).isoformat())
            else:
                for b in _session_hour_buckets(d):
                    out.add(b.isoformat())
        d += timedelta(days=1)
    return out
