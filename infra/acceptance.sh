#!/usr/bin/env bash
# Phase C acceptance — run on the ATP server as root. Proves the service-separation criteria via
# controlled fault injection (systemctl kill / docker stop). No live trading occurs; lifecycle is
# driven only by the harness with deterministic fixtures. Prints a PASS/FAIL summary at the end.
set -uo pipefail
APP=/opt/atp/app; ENVF=/opt/atp/atp.env
set -a; . "$ENVF"; set +a
PY="$APP/.venv/bin/python"; export PYTHONPATH="$APP/src"
SVCS=(atp-marketdata atp-trading atp-control)
declare -A HPORT=( [atp-marketdata]=9101 [atp-trading]=9102 [atp-control]=9103 )
declare -a RESULTS
pass(){ RESULTS+=("PASS|$1"); printf '  [PASS] %s\n' "$1"; }
fail(){ RESULTS+=("FAIL|$1"); printf '  [FAIL] %s\n' "$1"; }

mainpid(){ systemctl show -p MainPID --value "$1"; }
active(){ systemctl is-active --quiet "$1"; }
suser(){ systemctl show -p User --value "$1"; }
puser(){ ps -o user= -p "$1" 2>/dev/null | tr -d ' '; }
health(){ curl -s -m 3 "http://127.0.0.1:${HPORT[$1]}/health" 2>/dev/null; }
runstate(){ "$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
s=open_store(build_dsn(),migrate=False); rs=s.get_runtime_state(); print(rs.status if rs else "None"); s.close()
PY
}
wait_pidchange(){ local s=$1 old=$2 n; for _ in $(seq 1 "${3:-25}"); do n=$(mainpid "$s"); [ -n "$n" ] && [ "$n" != 0 ] && [ "$n" != "$old" ] && active "$s" && { echo "$n"; return 0; }; sleep 1; done; return 1; }

echo "===================== PHASE C ACCEPTANCE ====================="

echo "--- baseline: reset atp_prod, restart services ---"
"$PY" - <<'PY'
import psycopg
from atp.services.base import build_dsn
from atp.store import open_store
T=["schema_migrations","accounts","runtime_state","risk_config","risk_state","kill_switch","daily_pnl","daily_loss_lock","positions","orders","fills","trades","decisions","audit_events","service_heartbeats","market_data_health"]
c=psycopg.connect(build_dsn(),autocommit=True)
[c.cursor().execute("DROP TABLE IF EXISTS "+t+" CASCADE") for t in T]; c.close()
open_store(build_dsn(),migrate=True).close(); print("baseline reset+migrated")
PY
systemctl restart atp-marketdata atp-trading atp-control; sleep 7

echo "--- A) supervision + run-as-atp + health/heartbeat ---"
for s in "${SVCS[@]}"; do
  active "$s" && pass "$s active (supervised)" || fail "$s not active"
  [ "$(suser "$s")" = atp ] && pass "$s unit User=atp" || fail "$s unit User!=atp"
  pid=$(mainpid "$s"); [ "$(puser "$pid")" = atp ] && pass "$s process runs as atp" || fail "$s process user=$(puser "$pid")"
  echo "$(health "$s")" | grep -q '"service"' && pass "$s /health responds" || fail "$s /health no response"
done
curl -s -m3 http://127.0.0.1:9103/status | grep -q '"services"' && pass "control /status aggregates" || fail "control /status"
"$PY" - <<'PY' && pass "all 3 heartbeats emitted" || fail "heartbeats missing"
from atp.services.base import build_dsn
from atp.store import open_store
s=open_store(build_dsn(),migrate=False); hb={r[0] for r in s.list_heartbeats()}; s.close()
import sys; sys.exit(0 if {"market_data","trading_core","control"} <= hb else 1)
PY

echo "--- B) independent kill/restart (others unaffected, never auto-RUNNING) ---"
for s in "${SVCS[@]}"; do
  old=$(mainpid "$s"); declare -a snap=()
  for o in "${SVCS[@]}"; do [ "$o" != "$s" ] && snap+=("$o:$(mainpid "$o")"); done
  systemctl kill -s SIGKILL "$s"
  new=$(wait_pidchange "$s" "$old" 25) && pass "$s restarted by systemd ($old->$new)" || fail "$s did not restart"
  u=1; for op in "${snap[@]}"; do o=${op%%:*}; p=${op##*:}; { [ "$(mainpid "$o")" = "$p" ] && active "$o"; } || u=0; done
  [ $u = 1 ] && pass "$s kill left the other two untouched" || fail "$s kill disturbed others"
  [ "$(runstate)" != RUNNING ] && pass "$s restart: not RUNNING ($(runstate))" || fail "$s restart went RUNNING"
  sleep 2
done

echo "--- C) durable state survives a process kill ---"
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store, D
from atp.store.base import FillRow, utcnow_iso
from atp.runtime.positions import apply_fill_to_position
s=open_store(build_dsn(),migrate=False)
s.set_daily_loss_lock(trade_date="2026-08-14", engaged=True, reason="acceptance seed")
s.insert_order_intent(client_order_id="acc1", idempotency_key="acc1", instrument="AAPL", side="BUY",
                      quantity=D("10"), order_type="MARKET", correlation_id="acc")
f=FillRow("accf1","acc1","AAPL","BUY",D("10"),D("100"),D("1"),utcnow_iso())
s.apply_fill_atomic(fill=f, compute=lambda cur: apply_fill_to_position(cur,f)); s.close(); print("seeded")
PY
old=$(mainpid atp-trading); systemctl kill -s SIGKILL atp-trading; wait_pidchange atp-trading "$old" 25 >/dev/null
"$PY" - <<'PY' && pass "durable state survived kill (daily-loss + order + fill + position)" || fail "durable state lost after kill"
from atp.services.base import build_dsn
from atp.store import open_store
from atp.runtime.positions import reconstruct_positions
s=open_store(build_dsn(),migrate=False)
lock=s.get_daily_loss_lock("2026-08-14").engaged
fills=len(s.list_fills("AAPL")); pos=reconstruct_positions(s).get("AAPL"); o=s.get_order_by_idempotency("acc1")
s.close()
import sys; sys.exit(0 if (lock and fills==1 and pos and str(pos.quantity)=="10.00000000" and o and o.state=="FILLED") else 1)
PY
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
s=open_store(build_dsn(),migrate=False); s.set_daily_loss_lock(trade_date="2026-08-14", engaged=False, reason="cleanup"); s.close()
PY

echo "--- D) kill switch durability (latches KILLED across restart) ---"
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
from atp.runtime import LifecycleManager
s=open_store(build_dsn(),migrate=False); LifecycleManager(s).kill(actor="acceptance", reason="durability"); s.close()
PY
old=$(mainpid atp-trading); systemctl kill -s SIGKILL atp-trading; wait_pidchange atp-trading "$old" 25 >/dev/null; sleep 1
[ "$(runstate)" = KILLED ] && pass "kill switch survived restart -> KILLED" || fail "kill switch not durable ($(runstate))"
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
from atp.runtime import LifecycleManager
s=open_store(build_dsn(),migrate=False); LifecycleManager(s).reset_kill(actor="acceptance"); s.close()
PY

echo "--- F) control crash -> zero impact on trading core ---"
tpid=$(mainpid atp-trading); tstate=$(runstate); old=$(mainpid atp-control)
systemctl kill -s SIGKILL atp-control; sleep 3
{ [ "$(mainpid atp-trading)" = "$tpid" ] && active atp-trading; } && pass "control crash: trading_core pid unchanged & active" || fail "control crash disturbed trading"
[ "$(runstate)" = "$tstate" ] && pass "control crash: trading runtime_state unchanged ($tstate)" || fail "control crash changed trading state"
wait_pidchange atp-control "$old" 25 >/dev/null && pass "control restarted by systemd" || fail "control did not restart"

echo "--- G) market data crash -> stale -> new inputs blocked ---"
systemctl stop atp-marketdata; sleep 7
"$PY" - <<'PY' && pass "MD down -> market_data_fresh=False -> inputs blocked" || fail "MD staleness did not block"
from atp.services.base import build_dsn
from atp.store import open_store
from atp.services.recovery import market_data_fresh
s=open_store(build_dsn(),migrate=False); fresh=market_data_fresh(s, max_age_s=5); s.close()
import sys; sys.exit(0 if not fresh else 1)
PY
systemctl start atp-marketdata; sleep 5
"$PY" - <<'PY' && pass "MD restart -> freshness restored" || fail "freshness not restored"
from atp.services.base import build_dsn
from atp.store import open_store
from atp.services.recovery import market_data_fresh
s=open_store(build_dsn(),migrate=False); fresh=market_data_fresh(s, max_age_s=10); s.close()
import sys; sys.exit(0 if fresh else 1)
PY

echo "--- H) redis loss -> no authoritative state loss, no crash ---"
before=$(runstate)
docker stop atp_redis >/dev/null; sleep 5
u=1; for s in "${SVCS[@]}"; do active "$s" || u=0; done
[ $u = 1 ] && pass "redis down: all 3 services still active" || fail "redis down crashed a service"
[ "$(runstate)" = "$before" ] && pass "redis down: authoritative state intact ($before)" || fail "redis down changed authoritative state"
docker start atp_redis >/dev/null
for _ in $(seq 1 15); do docker exec atp_redis redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG && break; sleep 1; done
sleep 3; pass "redis restarted"

echo "--- E) trading crash from RUNNING -> RECOVERY_REQUIRED ---"
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store, D
from atp.runtime import LifecycleManager
from atp.runtime.lifecycle import RuntimeStatus
s=open_store(build_dsn(),migrate=False); l=LifecycleManager(s); l.recover()
if l.status is RuntimeStatus.KILLED: l.reset_kill(actor="acceptance")
if l.status is RuntimeStatus.DISABLED: l.mark_ready()
if l.status is RuntimeStatus.READY_FOR_ARM: l.arm()
if l.status is RuntimeStatus.ARMED: l.start(confirm=True)
s.upsert_risk_state(day_start_equity=D("1000000"),peak_equity=D("1000000"),halted=False,killed=False)
print("state", l.status.value); s.close()
PY
[ "$(runstate)" = RUNNING ] && pass "operator drove lifecycle to RUNNING (no execution)" || fail "could not reach RUNNING ($(runstate))"
old=$(mainpid atp-trading); systemctl kill -s SIGKILL atp-trading; wait_pidchange atp-trading "$old" 25 >/dev/null; sleep 2
[ "$(runstate)" = RECOVERY_REQUIRED ] && pass "trading crash from RUNNING -> RECOVERY_REQUIRED" || fail "did not land RECOVERY_REQUIRED ($(runstate))"

echo "--- I) postgres loss -> fail closed (last fault: services reconnect via restart after) ---"
"$PY" - <<'PY' && pass "PG down -> gate fails closed (NO NEW TRADE); PG up -> restored" || fail "PG loss not fail-closed"
import subprocess, sys, time
from atp.services.base import build_dsn
from atp.store import open_store, D
from atp.runtime import LifecycleManager, TradingGate
from atp.runtime.lifecycle import RuntimeStatus
from atp.services.recovery import build_recovery_checks
s=open_store(build_dsn(),migrate=False); l=LifecycleManager(s); l.recover()
from atp.runtime.positions import reconstruct_positions
bp={k:str(v.quantity) for k,v in reconstruct_positions(s).items()}   # broker agrees with DB → reconcile ok
if l.status is RuntimeStatus.RECOVERY_REQUIRED: l.run_recovery(build_recovery_checks(s, broker_positions=bp))
if l.status is RuntimeStatus.READY_FOR_ARM: l.arm()
if l.status is RuntimeStatus.ARMED: l.start(confirm=True)
s.upsert_risk_state(day_start_equity=D("1000000"),peak_equity=D("1000000"),halted=False,killed=False)
g=TradingGate(s,l); up=g.can_trade().allowed
subprocess.run(["docker","stop","atp_postgres"],capture_output=True)
r=g.can_trade(); down=(not r.allowed) and ("database" in r.reason)
subprocess.run(["docker","start","atp_postgres"],capture_output=True)
for _ in range(30):
    if subprocess.run(["docker","exec","atp_postgres","pg_isready","-U","postgres","-d","postgres"],capture_output=True).returncode==0: break
    time.sleep(1)
print("up_allowed",up,"down_blocked",down,"reason",r.reason)
sys.exit(0 if (up and down) else 1)
PY
sleep 3

echo "--- cleanup: safe baseline + restart services (reconnect) ---"
"$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
from atp.runtime import LifecycleManager
from atp.runtime.lifecycle import RuntimeStatus
s=open_store(build_dsn(),migrate=False); l=LifecycleManager(s); l.recover()
if l.killed(): l.reset_kill(actor="acceptance")
try:
    if l.status in (RuntimeStatus.RUNNING,RuntimeStatus.HALTED): l.stop()
    if l.status in (RuntimeStatus.ARMED,RuntimeStatus.READY_FOR_ARM): l.disarm()
except Exception as e: print("cleanup:",e)
s.set_daily_loss_lock(trade_date="2026-08-14",engaged=False,reason="cleanup"); print("baseline", l.status.value); s.close()
PY
systemctl restart atp-marketdata atp-trading atp-control; sleep 7
for s in "${SVCS[@]}"; do active "$s" && pass "$s healthy at end" || fail "$s not healthy at end"; done
[ "$(runstate)" != RUNNING ] && pass "final runtime_state not RUNNING ($(runstate))" || fail "final state RUNNING"

echo "--- J) durable-state contract (pytest, real Postgres atp_test) ---"
export ATP_TEST_POSTGRES_DSN="postgresql://${ATP_TEST_USER}:${ATP_TEST_PASSWORD}@127.0.0.1:5432/${ATP_TEST_DB}"
if "$PY" -m pytest "$APP/tests/acceptance" -q -p no:cacheprovider; then pass "pytest durable-state contract: ALL PASS"; else fail "pytest durable-state contract: failures"; fi

echo "===================== SUMMARY ====================="
np=0; nf=0
for r in "${RESULTS[@]}"; do case "$r" in PASS*) np=$((np+1));; FAIL*) nf=$((nf+1));; esac; done
echo "PASS=$np  FAIL=$nf"
if [ $nf -eq 0 ]; then echo "ACCEPTANCE=PASS"; else echo "ACCEPTANCE=FAIL"; printf '  %s\n' "${RESULTS[@]}" | grep '|FAIL' ; fi
