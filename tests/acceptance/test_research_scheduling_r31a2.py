"""§ R3.1A.2 acceptance — the research workers are actually SCHEDULED and actually CONFIGURED.

Proves: the one-shot workers resolve the production DB configuration (ATP_DATABASE_URL / ATP_APP_*) and
fail CLOSED (exit 2, no secret printed) when nothing is configured; the systemd one-shot services + timers
exist, are repository-standard, run collection inside the real post-close window for BOTH US DST offsets
and early closes, and are installed + enabled by the deploy script; the read-only scheduling/last-event
status read model reports observed activity honestly (never invented); and a validation run summary carries
`gate_passed` so no consumer can claim a positive verdict without a passed preregistered gate.

RESEARCH DATA ONLY: nothing here trades, enables execution, or touches broker/orders/credentials.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.intel import dsn, policy
from atp.research.intel import readmodel as intel_read
from atp.research.intel import worker as intel_worker
from atp.research.validation import readmodel as val_read
from atp.research.validation import worker as val_worker
from atp.store import open_store

REPO = Path(__file__).resolve().parents[2]
SYSTEMD = REPO / "infra" / "systemd"
SHA = "a" * 40
SECRET = "sup3r-s3cret-pw"

UNITS = {
    "collect": ("atp-research-intel-collect", "atp.research.intel.worker collect"),
    "evaluate": ("atp-research-intel-evaluate", "atp.research.intel.worker evaluate"),
    "validate": ("atp-research-validation", "atp.research.validation.worker run"),
}


def _store(path=None):
    return open_store(path or (str(Path(tempfile.mkdtemp()) / "atp.db")))


@pytest.fixture
def clean_env(monkeypatch):
    for var in (*dsn.URL_VARS, *dsn.COMPONENT_VARS, "ATP_REPO_DIR", "ATP_COMMIT_REF"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _on_calendar(unit_file: Path) -> list[str]:
    return [ln.split("=", 1)[1].strip() for ln in unit_file.read_text().splitlines()
            if ln.strip().startswith("OnCalendar=")]


def _timer_utc(spec: str, d) -> datetime:
    """The UTC instant a timer entry fires on session date `d` (host time is pinned to UTC)."""
    hh, mm, ss = (int(x) for x in re.search(r"(\d{2}):(\d{2}):(\d{2})", spec).groups())
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=timezone.utc)


# --------------------------------------------------------------- store-URL resolution (fail closed)
def test_resolver_accepts_the_production_variables_in_explicit_precedence():
    env = {"ATP_STORE_URL": "postgresql://a/1", "DATABASE_URL": "postgresql://b/2",
           "ATP_DATABASE_URL": "postgresql://c/3"}
    assert dsn.resolve_store_url(env) == "postgresql://a/1"
    assert dsn.describe_source(env) == "ATP_STORE_URL"
    env.pop("ATP_STORE_URL")
    assert dsn.resolve_store_url(env) == "postgresql://b/2"
    env.pop("DATABASE_URL")
    # The production variable the units actually ship — the case that used to exit 2 forever.
    assert dsn.resolve_store_url(env) == "postgresql://c/3"
    assert dsn.describe_source(env) == "ATP_DATABASE_URL"


def test_resolver_assembles_the_supervised_service_components():
    env = {"ATP_APP_USER": "atp_app", "ATP_APP_PASSWORD": SECRET, "ATP_PROD_DB": "atp_prod"}
    assert dsn.resolve_store_url(env) == f"postgresql://atp_app:{SECRET}@127.0.0.1:5432/atp_prod"
    env["ATP_PG_HOST"], env["ATP_PG_PORT"] = "db.internal", "6543"
    assert dsn.resolve_store_url(env).endswith("@db.internal:6543/atp_prod")
    assert dsn.describe_source(env) == "ATP_APP_* components"


def test_resolver_fails_closed_and_never_names_a_secret():
    assert dsn.resolve_store_url({}) is None                    # never a guessed default
    assert dsn.resolve_store_url({"ATP_APP_USER": "atp_app"}) is None   # user without password → closed
    assert dsn.describe_source({}) == "NONE"
    blob = json.dumps(dsn.missing_config_reason())               # diagnostic lists variable NAMES only
    assert "NO_STORE_URL" in blob and SECRET not in blob and "://" not in blob
    # describe_source reports a variable NAME, never its value.
    assert SECRET not in dsn.describe_source({"ATP_APP_PASSWORD": SECRET})


@pytest.mark.parametrize("main,argv", [(intel_worker.main, ["collect"]), (intel_worker.main, ["evaluate"]),
                                       (val_worker.main, ["run"])])
def test_workers_exit_2_without_db_config_and_leak_no_secret(clean_env, capsys, main, argv):
    assert main(argv) == 2                                      # fail closed BEFORE any DB/commit work
    out = capsys.readouterr().out
    assert json.loads(out)["error"] == "NO_STORE_URL"
    assert SECRET not in out


def test_collect_worker_uses_the_production_atp_database_url(clean_env, capsys):
    """The regression: a correctly installed unit ships ATP_DATABASE_URL, which the worker used to ignore."""
    dbfile = str(Path(tempfile.mkdtemp()) / "atp.db")
    _store(dbfile).close()                                      # migrate once, as the deploy does
    clean_env.setenv("ATP_DATABASE_URL", dbfile)
    # Outside the post-close window: the worker still runs end-to-end and records an HONEST skip.
    rc = intel_worker.main(["collect"], _now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc), _commit_sha=SHA)
    body = json.loads(capsys.readouterr().out)
    assert rc == 0 and body["ok"] is True and body["eligible"] is False
    assert body["reason"] == "AFTER_COLLECTION_WINDOW" and body["store_source"] == "ATP_DATABASE_URL"
    store = _store(dbfile)
    assert store.ri_last_event().event_type == "SAMPLE_SKIPPED"   # honest skip, never a backdated sample


# ------------------------------------------------------------------------- systemd units + timers
@pytest.mark.parametrize("job", sorted(UNITS))
def test_one_shot_service_units_are_repository_standard(job):
    name, command = UNITS[job]
    text = (SYSTEMD / f"{name}.service").read_text()
    assert "Type=oneshot" in text
    assert "User=atp" in text and "Group=atp" in text
    assert f"ExecStart=/opt/atp/app/.venv/bin/python -m {command}" in text
    assert "EnvironmentFile=/opt/atp/atp.env" in text            # where ATP_DATABASE_URL / ATP_APP_* live
    assert "Environment=PYTHONPATH=/opt/atp/app/src" in text
    # The data stack runs in Docker on this host — ordering after a non-existent postgresql.service would
    # be a silent no-op, and the Persistent=true catch-up runs would fire before the DB container is up.
    assert "docker.service" in text and "postgresql.service" not in text
    for hardening in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true"):
        assert hardening in text
    assert "Restart=" not in text                                # one-shot: no supervision loop
    # RESEARCH ONLY — a research unit never turns on trading/execution.
    assert "BROKER_EXECUTION_ENABLED=true" not in text and "ATP_AUTONOMOUS" not in text


@pytest.mark.parametrize("job", sorted(UNITS))
def test_timers_bind_their_service_and_match_the_declared_schedule(job):
    name, _ = UNITS[job]
    text = (SYSTEMD / f"{name}.timer").read_text()
    assert f"Unit={name}.service" in text
    assert "WantedBy=timers.target" in text
    declared = {"collect": intel_read.COLLECT_ONCALENDAR, "evaluate": intel_read.EVALUATE_ONCALENDAR,
                "validate": intel_read.VALIDATE_ONCALENDAR}[job]
    assert _on_calendar(SYSTEMD / f"{name}.timer") == list(declared)


def test_collection_timer_is_ordered_before_evaluation_and_validation():
    """Collection → evaluation → validation, both by unit ordering and by wall-clock."""
    assert "After=" in (SYSTEMD / "atp-research-intel-evaluate.service").read_text()
    assert "atp-research-intel-collect.service" in (SYSTEMD / "atp-research-intel-evaluate.service").read_text()
    assert "atp-research-intel-evaluate.service" in (SYSTEMD / "atp-research-validation.service").read_text()
    def hhmmss(spec):
        return re.search(r"\d{2}:\d{2}:\d{2}", spec).group(0)

    assert max(hhmmss(s) for s in intel_read.COLLECT_ONCALENDAR) < hhmmss(intel_read.EVALUATE_ONCALENDAR[0])
    assert hhmmss(intel_read.EVALUATE_ONCALENDAR[1]) < hhmmss(intel_read.VALIDATE_ONCALENDAR[0])  # 06:10→06:40


def test_collection_timer_fires_inside_the_real_post_close_window(monkeypatch):
    """Collection must be eligible on a regular EDT session, a regular EST session and an early close —
    otherwise every run would be an honest skip and the pilot would never collect a single sample."""
    from datetime import date
    for d in (date(2026, 8, 20),          # regular session, EDT (close 20:00 UTC)
              date(2026, 1, 15),          # regular session, EST (close 21:00 UTC)
              date(2025, 7, 3),           # early close, EDT (close 17:00 UTC)
              date(2026, 11, 27)):        # early close, EST (close 18:00 UTC)
        assert cal.is_session_day(d)
        hits = [s for s in intel_read.COLLECT_ONCALENDAR
                if policy.eligible_session(_timer_utc(s, d))["eligible"]
                and policy.eligible_session(_timer_utc(s, d))["session_date"] == d.isoformat()]
        assert len(hits) >= 2, f"no in-window collection attempt for {d} (retry pair required)"


def test_collection_timer_never_randomizes_out_of_its_window():
    text = (SYSTEMD / "atp-research-intel-collect.timer").read_text()
    # a random delay could push the run past its window; a catch-up run is always outside it
    assert re.search(r"^RandomizedDelaySec=", text, re.MULTILINE) is None
    assert "Persistent=false" in text


def test_deploy_installs_and_enables_every_research_unit_without_enabling_trading():
    sh = (REPO / "infra" / "deploy_services.sh").read_text()
    for name, _ in UNITS.values():
        assert f"{name}.service" in sh and f"{name}.timer" in sh
    enable = sh[sh.index("systemctl daemon-reload"):]
    for name, _ in UNITS.values():
        assert f"{name}.timer" in enable, f"{name}.timer is installed but never enabled"
    # The one-shot collect service is NOT started at deploy time (it would be outside its window).
    assert "systemctl start atp-research-intel-collect.service" not in sh
    assert "BROKER_EXECUTION_ENABLED=false" in sh          # trading stays disabled
    assert "ATP_DURABLE_PAPER_CANARY_ENABLED=false" in sh


def test_env_example_documents_the_worker_configuration():
    env = (REPO / "infra" / "atp.env.example").read_text()
    assert "ATP_DATABASE_URL" in env and "ATP_APP_PASSWORD" in env
    assert "fail closed" in env


# --------------------------------------------------------------- read-only scheduling / last-event status
def test_status_view_reports_an_empty_pilot_honestly():
    v = intel_read.schedule_status_view(_store())
    assert v["collection"] == {"snapshot_count": 0, "session_count": 0, "latest_session_date": None,
                               "latest_created_at": None}
    assert v["outcomes"]["outcome_count"] == 0 and v["outcomes"]["latest_evaluation_ts"] is None
    assert v["last_event"] is None and v["recent_events"] == [] and v["last_snapshot_event"] is None
    assert v["latest_validation_run"] is None                    # no run ⇒ no verdict, never invented
    assert v["safety"] == {"research_only": True, "autonomous": "DISABLED", "execution": "DISABLED",
                           "ibkr_orders": 0}
    jobs = {j["job"]: j for j in v["schedule"]["jobs"]}
    assert set(jobs) == {"collect", "evaluate", "validate"} and v["schedule"]["timezone"] == "UTC"
    assert jobs["collect"]["unit"] == "atp-research-intel-collect"
    assert v["schedule"]["window"]["post_close_window_minutes"] == policy.POST_CLOSE_WINDOW_MINUTES


def test_status_view_surfaces_the_last_collection_event_and_counts():
    store = _store()
    for i, et in enumerate(("SAMPLE_SKIPPED", "UNSUPPORTED_MARKET", "SNAPSHOT_WRITTEN")):
        store.ri_add_event({"id": f"e{i}", "event_type": et, "severity": "INFO", "symbol": "NVDA",
                            "session_date": "2026-08-20", "commit_sha": SHA, "details": {"i": i}})
    v = intel_read.schedule_status_view(store, event_limit=2)
    assert len(v["recent_events"]) == 2
    assert v["last_event"]["event_type"] == "SNAPSHOT_WRITTEN"
    assert v["last_event"]["details"] == {"i": 2} and v["last_event"]["commit_sha"] == SHA
    assert v["last_snapshot_event"]["id"] == "e2"
    assert store.ri_last_event("SAMPLE_SKIPPED").event_type == "SAMPLE_SKIPPED"
    assert store.ri_last_event("NO_SUCH_EVENT") is None
    assert [e.event_type for e in store.ri_list_events(limit=1)] == ["SNAPSHOT_WRITTEN"]


def test_status_view_separates_the_last_write_from_the_trailing_out_of_window_skip():
    """The real EDT sequence: collection succeeds at 20:20 UTC, then the EST attempts at 21:20/21:35 fire
    out of window and record honest skips. `last_event` is therefore a skip on a HEALTHY day — the
    operator's liveness signal is `last_snapshot_event`, which must still show the write."""
    store = _store()
    # Ids ascend with insertion order so the `created_at DESC, id DESC` tie-break stays deterministic.
    store.ri_add_event({"id": "e0", "event_type": "SNAPSHOT_WRITTEN", "severity": "INFO", "symbol": "NVDA",
                        "session_date": "2026-08-20", "commit_sha": SHA, "details": {}})
    for i in (1, 2):
        store.ri_add_event({"id": f"e{i}", "event_type": "SAMPLE_SKIPPED", "severity": "INFO",
                            "session_date": "2026-08-20", "commit_sha": SHA,
                            "details": {"reason": "AFTER_COLLECTION_WINDOW"}})
    v = intel_read.schedule_status_view(store)
    assert v["last_event"]["event_type"] == "SAMPLE_SKIPPED"      # honest, but NOT a liveness signal
    assert v["last_snapshot_event"]["id"] == "e0"                 # the pilot is demonstrably collecting


def test_status_view_is_read_only():
    store = _store()
    intel_read.schedule_status_view(store)
    assert store.ri_count_events() == 0 and store.ri_list_snapshots() == []


# ------------------------------------------------------------------------- control API (GET-only)
def _control(store):
    from atp.services import control
    control.ctx.store = store
    return control


def test_status_endpoint_is_read_only_and_bounded():
    store = _store()
    store.ri_add_event({"event_type": "SAMPLE_SKIPPED", "severity": "INFO", "session_date": "2026-08-20",
                        "commit_sha": SHA, "details": {"reason": "AFTER_COLLECTION_WINDOW"}})
    c = _control(store)
    body = c.research_intel_status()
    assert body["last_event"]["event_type"] == "SAMPLE_SKIPPED"
    assert body["safety"]["execution"] == "DISABLED" and body["safety"]["research_only"] is True
    assert len(c.research_intel_status(events=99999)["recent_events"]) == 1     # bounded, never unbounded
    assert store.ri_count_events() == 1                                        # the read wrote nothing


def test_no_http_write_path_can_trigger_a_research_worker():
    c = _control(_store())
    for route in c.app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set()) or set()
        if path.startswith(("/research/intel", "/research/validation")):
            assert methods <= {"GET", "HEAD"}, f"{path} exposes {methods} — workers are systemd-only"


# ----------------------------------------------------------------- gate_passed on the run read models
def test_gate_passed_is_never_optimistic():
    assert intel_read.gate_passed(json.dumps({"passed": True})) is True
    assert intel_read.gate_passed(json.dumps({"passed": False})) is False
    assert intel_read.gate_passed(None) is None                  # no report ⇒ UNKNOWN ⇒ not validated
    assert intel_read.gate_passed("not json") is None
    assert intel_read.gate_passed(json.dumps({"passed": "yes"})) is None   # non-bool is NOT a pass
    assert intel_read.gate_passed(json.dumps([1, 2])) is None


def test_run_summary_carries_gate_passed_so_a_verdict_cannot_be_claimed_without_it():
    store = _store()
    store.rv_create_run(run_id="RV1", universe_id=policy.UNIVERSE_ID, universe_version=policy.UNIVERSE_VERSION,
                        validation_policy_version=policy.VALIDATION_POLICY_VERSION,
                        outcome_policy_version=policy.OUTCOME_POLICY_VERSION,
                        sampling_policy_version=policy.SAMPLING_POLICY_VERSION, gate_id=policy.GATE_ID,
                        commit_sha=SHA)
    running = val_read.runs_view(store.rv_list_runs())["runs"][0]
    assert running["status"] == "RUNNING" and running["gate_passed"] is None
    store.rv_finalize_run("RV1", expected_from="RUNNING", status="INSUFFICIENT",
                          gate_report_json=json.dumps({"passed": False, "criteria": {}}))
    summary = val_read.runs_view(store.rv_list_runs())["runs"][0]
    assert summary["status"] == "INSUFFICIENT" and summary["gate_passed"] is False
    assert intel_read.schedule_status_view(store)["latest_validation_run"] == {
        "run_id": "RV1", "status": "INSUFFICIENT", "gate_passed": False,
        "created_at": summary["created_at"], "ended_at": summary["ended_at"]}
