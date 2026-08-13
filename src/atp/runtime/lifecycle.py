"""Runtime lifecycle with crash recovery (§ Phase B).

States: DISABLED · READY_FOR_ARM · ARMED · RUNNING · HALTED · KILLED · RECOVERY_REQUIRED.

The single most important rule: an unexpected restart must NEVER auto-resume RUNNING. If the process
died while ARMED / RUNNING / HALTED / KILLED, startup lands in RECOVERY_REQUIRED (KILLED stays KILLED).
Recovery runs a fixed sequence of checks and, only if ALL pass, moves to READY_FOR_ARM — never to
RUNNING. Human ARM is always required afterwards. Every transition persists runtime_state AND an
audit_event in one transaction (delegated to the Store).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum


class RuntimeStatus(str, Enum):
    DISABLED = "DISABLED"
    READY_FOR_ARM = "READY_FOR_ARM"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    KILLED = "KILLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


# States that mean "was mid-operation" — an unexpected restart from any of these is unsafe.
_ACTIVE_ON_CRASH = {RuntimeStatus.ARMED, RuntimeStatus.RUNNING,
                    RuntimeStatus.HALTED, RuntimeStatus.KILLED}
CONFIRM_PHRASE = "YES, START PAPER TRADING"

# The fixed 13-step recovery sequence (§ Phase B). Callers supply a checker per name.
RECOVERY_STEPS = [
    "load_runtime_state", "load_risk_state", "load_daily_pnl", "load_daily_loss_lock",
    "load_kill_switch", "load_positions", "load_orders", "load_fills", "query_broker",
    "reconcile", "validate_market_data", "validate_risk_service", "validate_database",
]


class LifecycleError(RuntimeError):
    pass


class LifecycleManager:
    """Durable state machine over the Store. Never holds authoritative state in memory — every read
    goes to the Store, every transition is persisted transactionally with its audit event."""

    def __init__(self, store):
        self._store = store

    # -- status --------------------------------------------------------------
    @property
    def status(self) -> RuntimeStatus:
        rs = self._store.get_runtime_state()
        return RuntimeStatus(rs.status) if rs else RuntimeStatus.DISABLED

    def killed(self) -> bool:
        return self._store.get_kill_switch().engaged

    # -- startup recovery ----------------------------------------------------
    def recover(self, *, actor: str = "system") -> RuntimeStatus:
        """Call once on process start. Applies the critical recovery rule."""
        kill = self._store.get_kill_switch()
        rs = self._store.get_runtime_state()

        # Kill switch is a durable latch: it survives restart and only a manual RESET clears it.
        if kill.engaged:
            if rs is None or rs.status != RuntimeStatus.KILLED.value:
                self._store.transition(new_status=RuntimeStatus.KILLED.value, actor=actor,
                                       reason="kill switch engaged (durable) on restart",
                                       action="RECOVERY_START", previous=(rs.status if rs else None))
            return RuntimeStatus.KILLED

        if rs is None:                       # first ever boot
            self._store.transition(new_status=RuntimeStatus.DISABLED.value, actor=actor,
                                   reason="first boot", action="INIT", previous=None)
            return RuntimeStatus.DISABLED

        prev = RuntimeStatus(rs.status)
        if prev in _ACTIVE_ON_CRASH:
            # Unexpected restart mid-operation → do NOT resume; require recovery.
            self._store.transition(new_status=RuntimeStatus.RECOVERY_REQUIRED.value, actor=actor,
                                   reason=f"unexpected restart from {prev.value}",
                                   action="RECOVERY_START", previous=prev.value)
            return RuntimeStatus.RECOVERY_REQUIRED
        # DISABLED / READY_FOR_ARM / RECOVERY_REQUIRED are safe, non-trading states → restore as-is.
        return prev

    def run_recovery(self, checks: dict[str, Callable[[], bool]], *, actor: str = "system"):
        """Run the recovery sequence. Returns (ok, results). On full pass → READY_FOR_ARM.
        On any failure → stays RECOVERY_REQUIRED. NEVER transitions to RUNNING."""
        if self.status is not RuntimeStatus.RECOVERY_REQUIRED:
            raise LifecycleError(f"run_recovery requires RECOVERY_REQUIRED (is {self.status.value})")
        results: list[tuple[str, bool]] = []
        for step in RECOVERY_STEPS:
            fn = checks.get(step)
            ok = False
            try:
                ok = bool(fn()) if fn is not None else False
            except Exception:
                ok = False
            results.append((step, ok))
            if not ok:
                self._store.audit(actor=actor, action="RECOVERY_FAIL",
                                  previous_state=RuntimeStatus.RECOVERY_REQUIRED.value,
                                  new_state=RuntimeStatus.RECOVERY_REQUIRED.value,
                                  reason=f"recovery check failed: {step}")
                return (False, results)
        # all passed → READY_FOR_ARM (human ARM still required); NEVER RUNNING
        self._store.transition(new_status=RuntimeStatus.READY_FOR_ARM.value, actor=actor,
                               reason="recovery checks passed", action="RECOVERY_PASS",
                               previous=RuntimeStatus.RECOVERY_REQUIRED.value)
        return (True, results)

    # -- guarded transitions -------------------------------------------------
    def _require(self, allowed: set[RuntimeStatus], what: str):
        if self.killed():
            raise LifecycleError(f"{what} blocked: kill switch engaged (manual RESET required)")
        st = self.status
        if st not in allowed:
            raise LifecycleError(f"{what} not allowed from {st.value}")
        return st

    def mark_ready(self, *, actor: str = "system", reason: str = "pre-arm validation passed") -> RuntimeStatus:
        st = self._require({RuntimeStatus.DISABLED}, "mark_ready")
        self._store.transition(new_status=RuntimeStatus.READY_FOR_ARM.value, actor=actor,
                               reason=reason, action="READY", previous=st.value)
        return RuntimeStatus.READY_FOR_ARM

    def arm(self, *, actor: str = "user") -> RuntimeStatus:
        st = self._require({RuntimeStatus.READY_FOR_ARM}, "arm")
        self._store.transition(new_status=RuntimeStatus.ARMED.value, actor=actor,
                               reason="armed", action="ARM", previous=st.value)
        return RuntimeStatus.ARMED

    def start(self, *, confirm, actor: str = "user") -> RuntimeStatus:
        st = self._require({RuntimeStatus.ARMED}, "start")
        if confirm is not True and confirm != CONFIRM_PHRASE:
            self._store.audit(actor=actor, action="START_REJECTED", previous_state=st.value,
                              new_state=st.value, reason="confirmation required (two-step activation)")
            raise LifecycleError("start requires explicit confirmation")
        self._store.transition(new_status=RuntimeStatus.RUNNING.value, actor=actor,
                               reason="explicit confirmation", action="START", previous=st.value)
        return RuntimeStatus.RUNNING

    def stop(self, *, actor: str = "user") -> RuntimeStatus:
        st = self._require({RuntimeStatus.RUNNING, RuntimeStatus.HALTED}, "stop")
        self._store.transition(new_status=RuntimeStatus.ARMED.value, actor=actor,
                               reason="stopped", action="STOP", previous=st.value)
        return RuntimeStatus.ARMED

    def disarm(self, *, actor: str = "user") -> RuntimeStatus:
        st = self._require({RuntimeStatus.ARMED, RuntimeStatus.READY_FOR_ARM, RuntimeStatus.HALTED}, "disarm")
        self._store.transition(new_status=RuntimeStatus.DISABLED.value, actor=actor,
                               reason="disarmed", action="DISARM", previous=st.value)
        return RuntimeStatus.DISABLED

    def halt(self, *, reason: str, actor: str = "risk") -> RuntimeStatus:
        # Halt is always permitted (risk-reducing), except when killed (already hard-stopped).
        if self.killed():
            return RuntimeStatus.KILLED
        st = self.status
        self._store.transition(new_status=RuntimeStatus.HALTED.value, actor=actor,
                               reason=reason, action="HALT", previous=st.value)
        return RuntimeStatus.HALTED

    # -- kill switch (durable) ----------------------------------------------
    def kill(self, *, actor: str = "user", reason: str = "kill switch") -> RuntimeStatus:
        prev = self.status
        # Latch first (durable), then reflect in runtime_state — both survive restart.
        self._store.set_kill_switch(engaged=True, actor=actor, reason=reason)
        self._store.transition(new_status=RuntimeStatus.KILLED.value, actor=actor,
                               reason=reason, action="KILL", previous=prev.value)
        return RuntimeStatus.KILLED

    def reset_kill(self, *, actor: str = "user", reason: str = "manual reset") -> RuntimeStatus:
        if self.status is not RuntimeStatus.KILLED and not self.killed():
            raise LifecycleError("reset_kill only valid from KILLED")
        self._store.set_kill_switch(engaged=False, actor=actor, reason=reason)
        self._store.transition(new_status=RuntimeStatus.DISABLED.value, actor=actor,
                               reason=reason, action="RESET", previous=RuntimeStatus.KILLED.value)
        return RuntimeStatus.DISABLED
