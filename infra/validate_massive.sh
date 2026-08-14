#!/usr/bin/env bash
# Phase E validation — run on the ATP server as root. Proves the REAL Massive realtime feed for
# AAPL/NVDA/SPY plus failure/reconnect behaviour and a secret non-exposure scan. MARKET DATA ONLY —
# no trading, no ARM, no execution. The Massive key value is never printed, logged, or copied.
# Per-symbol READY depends on live ticks (US market hours); the pipeline/resilience/secret checks do not.
set -uo pipefail
APP=/opt/atp/app; ENVF=/opt/atp/atp.env
set -a; . "$ENVF"; set +a
PY="$APP/.venv/bin/python"; export PYTHONPATH="$APP/src"
declare -a R
pass(){ R+=("PASS|$1"); printf '  [PASS] %s\n' "$1"; }
fail(){ R+=("FAIL|$1"); printf '  [FAIL] %s\n' "$1"; }
mainpid(){ systemctl show -p MainPID --value "$1"; }
active(){ systemctl is-active --quiet "$1"; }
runstate(){ "$PY" -c 'from atp.services.base import build_dsn; from atp.store import open_store; s=open_store(build_dsn(),migrate=False); rs=s.get_runtime_state(); print(rs.status if rs else None); s.close()'; }
feed(){ "$PY" - <<'PY'
from atp.services.base import redis_url
from atp.persistence.state import RedisStateStore
try:
    print((RedisStateStore(redis_url()).get("md:snapshot") or {}).get("feed"))
except Exception:
    print("REDIS_DOWN")
PY
}
mdhealth(){ "$PY" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
s=open_store(build_dsn(),migrate=False)
for r in s.list_md_health(): print(r[0], r[1], r[2])
s.close()
PY
}
wait_feed(){ local want=$1 t=$2 i; for i in $(seq 1 "$t"); do [ "$(feed)" = "$want" ] && return 0; sleep 1; done; return 1; }
wait_feed_not(){ local no=$1 t=$2 i f; for i in $(seq 1 "$t"); do f=$(feed); [ "$f" != "$no" ] && { echo "$f"; return 0; }; sleep 1; done; echo "$(feed)"; return 1; }

echo "===================== PHASE E — MASSIVE VALIDATION ====================="
echo "--- key presence (name only, no value) ---"
grep -qE '^MASSIVE_API_KEY=.+' "$ENVF" && pass "MASSIVE_API_KEY present server-side" || { fail "MASSIVE_API_KEY absent"; exit 1; }

echo "--- clean market_data_health baseline (drop stale Phase-C fixture rows) ---"
"$PY" - <<'PY'
import psycopg
from atp.services.base import build_dsn
c=psycopg.connect(build_dsn(),autocommit=True); c.cursor().execute("DELETE FROM market_data_health"); c.close()
print("  market_data_health cleared")
PY
systemctl restart atp-marketdata; sleep 3

echo "--- wait up to 45s for feed to connect ---"
wait_feed STREAMING 45 || true
FEED=$(feed); echo "feed=$FEED"
case "$FEED" in
  STREAMING)          pass "MASSIVE AUTH (auth_success, WS streaming)"; pass "ENTITLEMENT (subscribe accepted)";;
  ENTITLEMENT_FAILED) pass "MASSIVE AUTH (authenticated)"; fail "ENTITLEMENT (plan lacks realtime)";;
  AUTH_FAILED)        fail "MASSIVE AUTH (auth_failed — bad key)";;
  AUTH_MISSING)       fail "MASSIVE AUTH (key missing at runtime)";;
  *)                  fail "MASSIVE feed did not reach STREAMING (feed=$FEED)";;
esac

echo "--- allow up to 25s for live ticks to populate all symbols ---"
for i in $(seq 1 25); do
  RCNT=$("$PY" - <<'PY'
from atp.services.base import redis_url
from atp.persistence.state import RedisStateStore
try: d=RedisStateStore(redis_url()).get("md:snapshot") or {}
except Exception: d={}
s=d.get("symbols",{})
print(sum(1 for k in ("AAPL","NVDA","SPY") if (s.get(k,{}) or {}).get("status")=="READY"))
PY
)
  [ "$RCNT" = 3 ] && break; sleep 1
done

echo "--- per-symbol snapshot (bid/ask/last/latency; source/status/realtime — no secret) ---"
"$PY" - > /tmp/e_syms <<'PY'
from atp.services.base import redis_url
from atp.persistence.state import RedisStateStore
try: d=RedisStateStore(redis_url()).get("md:snapshot") or {}
except Exception: d={}
syms=d.get("symbols",{})
for k in ("AAPL","NVDA","SPY"):
    q=syms.get(k,{}) or {}
    print(k, q.get("status"), q.get("source"),
          f"rt={q.get('realtime')} bid={q.get('bid')} ask={q.get('ask')} last={q.get('last')} "
          f"bsz={q.get('bid_size')} asz={q.get('ask_size')} vol={q.get('volume')} lat_ms={q.get('latency_ms')} ts={q.get('timestamp')}")
PY
cat /tmp/e_syms
while read -r S ST SRC REST; do
  if [ "$ST" = READY ] && [ "$SRC" = MASSIVE ]; then pass "$S = MASSIVE/REALTIME/READY"
  else fail "$S = $SRC/$ST (not READY — thin/closed post-market; needs US market hours)"; fi
done < /tmp/e_syms

echo "--- FAILURE 1: kill market-data -> systemd restart -> reconnect ---"
old=$(mainpid atp-marketdata); systemctl kill -s SIGKILL atp-marketdata
for i in $(seq 1 25); do n=$(mainpid atp-marketdata); [ -n "$n" ] && [ "$n" != 0 ] && [ "$n" != "$old" ] && active atp-marketdata && break; sleep 1; done
{ [ "$(mainpid atp-marketdata)" != "$old" ] && active atp-marketdata; } && pass "market-data restarted by systemd" || fail "market-data did not restart"
wait_feed STREAMING 45 && pass "reconnect after restart (feed STREAMING)" || fail "did not reconnect (feed=$(feed))"

echo "--- FAILURE 2: block outbound 443 (Massive network interruption) ---"
iptables -I OUTPUT -p tcp --dport 443 -j DROP
NF=$(wait_feed_not STREAMING 60); echo "feed while blocked=$NF"
[ "$NF" != STREAMING ] && pass "network block: feed degraded ($NF)" || fail "feed still STREAMING while blocked"
"$PY" - <<'PY' && pass "stale gate: market_data_fresh=False -> Trading blocks inputs" || fail "stale gate did not block"
from atp.services.base import build_dsn
from atp.store import open_store
from atp.services.recovery import market_data_fresh
s=open_store(build_dsn(),migrate=False); f=market_data_fresh(s, max_age_s=8); s.close()
import sys; sys.exit(0 if not f else 1)
PY
mdhealth | grep -qi fixture && fail "NO-FALLBACK violated: FIXTURE in production health" || pass "NO FALLBACK: source stays MASSIVE (no fixture)"
iptables -D OUTPUT -p tcp --dport 443 -j DROP

echo "--- FAILURE 3: restore -> reconnect -> no auto-RUNNING ---"
wait_feed STREAMING 60 && pass "reconnect after network restore (feed STREAMING)" || fail "did not reconnect after restore (feed=$(feed))"
[ "$(runstate)" != RUNNING ] && pass "no automatic RUNNING after reconnect ($(runstate))" || fail "went RUNNING after reconnect"

echo "--- FAILURE 4: restart Redis -> recover, no authoritative state lost ---"
before=$(runstate); docker restart atp_redis >/dev/null; sleep 6
for i in $(seq 1 15); do docker exec atp_redis redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG && break; sleep 1; done
active atp-marketdata && pass "redis restart: market-data still active" || fail "redis restart crashed market-data"
[ "$(runstate)" = "$before" ] && pass "redis restart: authoritative runtime_state intact ($before)" || fail "redis restart changed authoritative state"
wait_feed STREAMING 30 && pass "redis restart: feed recovered" || fail "feed did not recover after redis (feed=$(feed))"

echo "--- FAILURE 5: restart Postgres -> fail closed, recover, no auto-RUNNING ---"
"$PY" - <<'PY' && pass "postgres down -> gate fails closed (NO NEW TRADE)" || fail "postgres down not fail-closed"
import subprocess, sys, time
from atp.services.base import build_dsn
from atp.store import open_store
from atp.runtime import LifecycleManager, TradingGate
s=open_store(build_dsn(),migrate=False); g=TradingGate(s, LifecycleManager(s))
subprocess.run(["docker","stop","atp_postgres"],capture_output=True)
r=g.can_trade(); blocked=(not r.allowed) and ("database" in r.reason)
subprocess.run(["docker","start","atp_postgres"],capture_output=True)
for _ in range(30):
    if subprocess.run(["docker","exec","atp_postgres","pg_isready","-U","postgres","-d","postgres"],capture_output=True).returncode==0: break
    time.sleep(1)
sys.exit(0 if blocked else 1)
PY
sleep 3; systemctl restart atp-marketdata atp-trading atp-control; sleep 6
[ "$(runstate)" != RUNNING ] && pass "postgres recover: no automatic RUNNING ($(runstate))" || fail "went RUNNING after postgres recover"

echo "--- SECRET SCAN (value read transiently into a var, never printed; unset after) ---"
KEY=$(grep -E '^MASSIVE_API_KEY=' "$ENVF" | cut -d= -f2-)
LEAK=0; [ -n "$KEY" ] || LEAK=2
journalctl -u atp-marketdata -u atp-trading -u atp-control --no-pager 2>/dev/null | grep -qF -- "$KEY" && LEAK=1
for p in 9101 9102 9103; do for ep in health ready status market; do curl -s -m3 "http://127.0.0.1:$p/$ep" 2>/dev/null | grep -qF -- "$KEY" && LEAK=1; done; done
redis-cli -a "$REDIS_PASSWORD" --scan 2>/dev/null | while read -r k; do redis-cli -a "$REDIS_PASSWORD" get "$k" 2>/dev/null; done | grep -qF -- "$KEY" && LEAK=1
git -C "$APP" grep -qF -- "$KEY" 2>/dev/null && LEAK=1
unset KEY
case $LEAK in 0) pass "SECRET SCAN: key value not exposed (journals/endpoints/redis/git)";; 1) fail "SECRET SCAN: key value LEAKED";; 2) fail "SECRET SCAN: key unreadable";; esac

echo "===================== SUMMARY ====================="
np=0; nf=0; for r in "${R[@]}"; do case "$r" in PASS*) np=$((np+1));; FAIL*) nf=$((nf+1));; esac; done
echo "PASS=$np FAIL=$nf FEED=$(feed) runtime=$(runstate)"
if [ $nf -eq 0 ]; then echo "PHASE_E=PASS"; else echo "PHASE_E=FAIL"; printf '  %s\n' "${R[@]}" | grep '|FAIL'; fi
