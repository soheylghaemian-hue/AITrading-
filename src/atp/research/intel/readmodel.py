"""§ R3.1A.2 — read-only scheduling / last-event status for the one-shot research workers.

The declared schedule below is the SINGLE SOURCE OF TRUTH shared by the systemd timers
(`infra/systemd/atp-research-*.timer`) and this read model, so the dashboard can never claim a cadence the
units do not actually run. Host time is pinned to UTC by `infra/bootstrap.sh`, so every `OnCalendar` here is
UTC. Collection fires INSIDE the narrow post-close window
`[close + SETTLE_MINUTES, +POST_CLOSE_WINDOW_MINUTES]` for both US DST offsets and for early closes; runs
outside the window are honest skips (`SAMPLE_SKIPPED`), never backdated samples.

RESEARCH DATA ONLY. This module reads; it never collects, evaluates, validates or trades, and there is no
HTTP write path that can trigger a worker.
"""
from __future__ import annotations

import json

from . import policy

_SAFETY = {"research_only": True, "autonomous": "DISABLED", "execution": "DISABLED", "ibkr_orders": 0}

#: Post-close collection attempts (UTC). 20:20/20:35 = 16:20/16:35 ET during EDT, 21:20/21:35 during EST;
#: 17:20/17:35 and 18:20/18:35 cover the 13:00 ET early close in both offsets. The second attempt of each
#: pair is an idempotent retry — a snapshot already written for the session is never duplicated.
COLLECT_ONCALENDAR: tuple[str, ...] = (
    "Mon-Fri *-*-* 17:20:00", "Mon-Fri *-*-* 17:35:00",     # early close, EDT
    "Mon-Fri *-*-* 18:20:00", "Mon-Fri *-*-* 18:35:00",     # early close, EST
    "Mon-Fri *-*-* 20:20:00", "Mon-Fri *-*-* 20:35:00",     # regular close, EDT
    "Mon-Fri *-*-* 21:20:00", "Mon-Fri *-*-* 21:35:00",     # regular close, EST
)
#: Maturation runs after the last collection window closes, plus a morning pass once overnight research
#: datasets have completed.
EVALUATE_ONCALENDAR: tuple[str, ...] = ("*-*-* 22:10:00", "*-*-* 06:10:00")
#: The validation run freezes whatever the morning evaluation matured.
VALIDATE_ONCALENDAR: tuple[str, ...] = ("*-*-* 06:40:00",)

SCHEDULE: dict = {
    "timezone": "UTC",
    "trigger": "systemd one-shot timers (no HTTP trigger, no in-process scheduler)",
    "jobs": [
        {"job": "collect", "unit": "atp-research-intel-collect", "command": "atp.research.intel.worker collect",
         "on_calendar": list(COLLECT_ONCALENDAR), "persistent": False,
         "note": "runs only inside the post-close window; outside it the session is honestly skipped"},
        {"job": "evaluate", "unit": "atp-research-intel-evaluate", "command": "atp.research.intel.worker evaluate",
         "on_calendar": list(EVALUATE_ONCALENDAR), "persistent": True,
         "note": "matures pending outcomes against COMPLETED immutable datasets only"},
        {"job": "validate", "unit": "atp-research-validation", "command": "atp.research.validation.worker run",
         "on_calendar": list(VALIDATE_ONCALENDAR), "persistent": True,
         "note": "freezes the matured set; COMPLETED only when the preregistered gate passes"},
    ],
    "window": {"settle_minutes": policy.SETTLE_MINUTES,
               "post_close_window_minutes": policy.POST_CLOSE_WINDOW_MINUTES,
               "calendar_version": policy.CALENDAR_VERSION, "exchange_tz": policy.EXCHANGE_TZ},
}


def _event_dict(e) -> dict:
    """One collection event as JSON. `details_json` is parsed defensively — a malformed blob is reported as
    an empty object rather than breaking the read model."""
    try:
        details = json.loads(e.details_json) if e.details_json else {}
    except (ValueError, TypeError):
        details = {}
    return {"id": e.id, "event_type": e.event_type, "severity": e.severity, "ts": e.ts, "symbol": e.symbol,
            "session_date": e.session_date, "snapshot_id": e.snapshot_id, "commit_sha": e.commit_sha,
            "created_at": e.created_at, "details": details}


def schedule_status_view(store, *, event_limit: int = 20) -> dict:
    """Declared schedule + observed collection/evaluation/validation footprint. READ-ONLY: it never writes a
    row, never starts a worker and never trades. Absent activity is reported as null/0, never invented."""
    events = store.ri_list_events(limit=event_limit)
    runs = store.rv_list_runs(limit=1)
    latest_run = runs[0] if runs else None
    # `last_event` alone is NOT a liveness signal: the timer deliberately fires attempts for both US DST
    # offsets, so the last attempt of an EDT session day is always an out-of-window SAMPLE_SKIPPED even
    # when collection succeeded minutes earlier. `last_snapshot_event` is the last actual write, so the
    # operator can tell a working pilot from a silent one without inferring it from a skip.
    last_written = store.ri_last_event("SNAPSHOT_WRITTEN")
    return {
        "schedule": SCHEDULE,
        "collection": store.ri_snapshot_summary(universe_id=policy.UNIVERSE_ID),
        "outcomes": store.ri_outcome_summary(),
        "last_event": _event_dict(events[0]) if events else None,
        "last_snapshot_event": _event_dict(last_written) if last_written else None,
        "recent_events": [_event_dict(e) for e in events],
        "latest_validation_run": None if latest_run is None else {
            "run_id": latest_run.run_id, "status": latest_run.status,
            "gate_passed": gate_passed(latest_run.gate_report_json),
            "created_at": latest_run.created_at, "ended_at": latest_run.ended_at},
        "safety": _SAFETY,
    }


def gate_passed(gate_report_json: str | None) -> bool | None:
    """`gate_report.passed` for a run, or None when no gate report exists (never optimistic: a missing or
    malformed report is UNKNOWN, and every consumer must treat UNKNOWN as not validated)."""
    if not gate_report_json:
        return None
    try:
        report = json.loads(gate_report_json)
    except (ValueError, TypeError):
        return None
    passed = report.get("passed") if isinstance(report, dict) else None
    return bool(passed) if isinstance(passed, bool) else None
