"""§ R3.1A — FROZEN, versioned policies for the AI-validation pilot (RESEARCH DATA ONLY).

Everything here is preregistered BEFORE collection and must not be tuned to fit results. It covers the
pilot universe, the canonical per-session sampling policy, the deterministic outcome policy, the precise
market-regime definition, the supported-market allowlist (fail-closed) and the reopen-R3.1 evidence gate.
No trading, no orders, no execution — this module only describes what to collect and how to score it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .. import calendars as cal

# ---- pilot universe (explicit; not the whole platform) --------------------------------------------
UNIVERSE_ID = "US_EQUITY_AI_VALIDATION_PILOT_V1"
UNIVERSE_VERSION = "v1"
PILOT_SYMBOLS: tuple[str, ...] = ("AAPL", "NVDA", "SPY")
ASSET_CLASS = "US_EQUITY"
EXCHANGE = "NYSE"
CURRENCY = "USD"
EXCHANGE_TZ = cal.EXCHANGE_TZ                    # "America/New_York"
CALENDAR_ID = cal.CALENDAR_ID                    # "NYSE"
CALENDAR_VERSION = cal.CALENDAR_VERSION          # "NYSE_2023_2027_V1"

# ---- supported-market allowlist (anything else FAILS CLOSED) --------------------------------------
SUPPORTED_MARKETS = frozenset({(ASSET_CLASS, EXCHANGE, CALENDAR_VERSION, EXCHANGE_TZ, CURRENCY)})


def is_supported_market(asset_class: str, exchange: str, calendar_version: str, tz: str, currency: str) -> bool:
    return (asset_class, exchange, calendar_version, tz, currency) in SUPPORTED_MARKETS


def is_supported_symbol(symbol: str) -> bool:
    return (symbol or "").upper() in PILOT_SYMBOLS

# ---- canonical sampling policy --------------------------------------------------------------------
SAMPLING_POLICY_VERSION = "US_EQUITY_PILOT_SESSION_CLOSE_V1"
# One canonical snapshot per symbol per COMPLETED NYSE session, anchored to the real session close
# (16:00 ET normal / 13:00 ET early close, DST-correct via the versioned calendar) + a fixed settle delay.
# The scheduled sampling TARGET is close+settle. Collection is permitted only within a NARROW post-target
# window; a worker outside it records an honest missed sample (§ correction 2) — it never stamps late data
# as if it existed at the target. The persisted `decision_ts` is the ACTUAL capture time, not the target.
SETTLE_MINUTES = 10
POST_CLOSE_WINDOW_MINUTES = 30   # collection allowed only within [target, target+30min]

# ---- outcome policy -------------------------------------------------------------------------------
OUTCOME_POLICY_VERSION = "US_EQUITY_RTH_OUTCOME_V1"
HORIZONS: tuple[int, ...] = (1, 3, 5, 20)         # trading sessions
NEUTRAL_THRESHOLD_PCT = Decimal("1.0")            # preregistered fixed band (NOT tuned to performance)
ADJUSTMENT_POLICY = "MASSIVE_SPLIT_ADJUSTED_RTH_V1"
NORMALIZATION_POLICY = "US_EQUITY_RTH_DAILY_FROM_1MIN_V1"
PROVIDER_CONTRACT_VERSION = "polygon-aggs-1min-chunked-v2"
DATASET_INTERVAL = "1D"

# ---- confidence semantics (heuristic, NOT a probability) ------------------------------------------
# Fixed descriptive buckets over the 0-100 heuristic confidence. Brier/log-loss/ECE are NOT APPLICABLE.
CONFIDENCE_BUCKETS: tuple[tuple[float, float], ...] = ((0, 40), (40, 55), (55, 70), (70, 85), (85, 100.0001))

# ---- market-regime definition (precise + versioned) ----------------------------------------------
REGIME_POLICY_VERSION = "US_EQUITY_TREND20_V1"
_REGIME_LOOKBACK = 20            # sessions
_REGIME_TREND_PCT = Decimal("5.0")


def classify_regime(trailing_return_pct: Decimal | None) -> str:
    """Deterministic regime from the 20-session trailing % return of the decision symbol (computed from the
    immutable OHLC dataset). UPTREND > +5%, DOWNTREND < -5%, else RANGE; UNKNOWN when history is short."""
    if trailing_return_pct is None:
        return "UNKNOWN"
    if trailing_return_pct > _REGIME_TREND_PCT:
        return "UPTREND"
    if trailing_return_pct < -_REGIME_TREND_PCT:
        return "DOWNTREND"
    return "RANGE"


REGIME_LOOKBACK = _REGIME_LOOKBACK

# ---- reopen-R3.1 evidence gate (frozen) -----------------------------------------------------------
GATE_ID = "VALIDATION_GATE_US_EQUITY_PILOT_V1"
GATE = {
    "min_unique_sessions": 252,
    "min_symbols": 3,
    "min_matured_outcomes_per_horizon": 200,
    "min_effective_samples_per_horizon": 200,      # session-level, not hourly
    "min_wall_clock_months": 12,
    "require_full_20_session_maturity": True,
    "min_distinct_regimes": 2,
    "regime_policy_version": REGIME_POLICY_VERSION,
    "max_unknown_provenance_fraction": 0.20,
    "max_missing_data_fraction": 0.20,
}
VALIDATION_POLICY_VERSION = "US_EQUITY_PILOT_VALIDATION_V1"


# ---- forward-only session derivation --------------------------------------------------------------
def last_completed_session(now: datetime):
    """The most recent NYSE session whose regular close is strictly in the past (never the in-progress
    day). Returns a date, or None if the clock is outside the versioned calendar coverage."""
    d = now.astimezone(cal.NY).date()
    for _ in range(0, 400):
        if d < cal.CALENDAR_START:
            return None
        if cal.is_session_day(d) and cal.session_close_utc(d) <= now:
            return d
        d = d - timedelta(days=1)
    return None


def eligible_session(now: datetime) -> dict:
    """Derive the eligible just-completed session for `now` (verified UTC), forward-only. Collection is
    permitted ONLY inside the narrow window [target, target+POST_CLOSE_WINDOW_MINUTES] where
    target = close+settle; outside it the session is honestly skipped (never reconstructed/backdated).
    Returns {eligible, session_date, scheduled_target_ts, is_early_close, reason}. The persisted decision_ts
    is the ACTUAL capture time (set by the collector), never the scheduled target."""
    now = now.astimezone(timezone.utc)
    d = last_completed_session(now)
    if d is None:
        return {"eligible": False, "reason": "NO_COMPLETED_SESSION_IN_CALENDAR"}
    close = cal.session_close_utc(d)
    target = close + timedelta(minutes=SETTLE_MINUTES)
    window_end = target + timedelta(minutes=POST_CLOSE_WINDOW_MINUTES)
    if now < target:
        return {"eligible": False, "session_date": d.isoformat(), "reason": "BEFORE_SETTLE_WINDOW"}
    if now > window_end:
        return {"eligible": False, "session_date": d.isoformat(), "reason": "AFTER_COLLECTION_WINDOW"}
    return {"eligible": True, "session_date": d.isoformat(), "scheduled_target_ts": target.isoformat(),
            "is_early_close": cal.is_early_close(d), "reason": None}


def expected_outcome_contract() -> dict:
    """The outcome-data contract a snapshot promises its future evaluation will honor — pinned as JSON at
    snapshot time (NO future dataset is pinned then; the dataset is pinned only after maturity)."""
    return {"interval": DATASET_INTERVAL, "provider_contract_version": PROVIDER_CONTRACT_VERSION,
            "adjustment_policy": ADJUSTMENT_POLICY, "calendar_id": CALENDAR_ID,
            "calendar_version": CALENDAR_VERSION, "horizons": list(HORIZONS),
            "neutral_threshold_pct": str(NEUTRAL_THRESHOLD_PCT), "outcome_policy_version": OUTCOME_POLICY_VERSION,
            "decision_price_rule": "decision-session 1D close", "outcome_price_rule": "close N sessions later",
            "price_source": "COMPLETED immutable research dataset only (never live ohlc_bars)"}
