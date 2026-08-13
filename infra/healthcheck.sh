#!/usr/bin/env bash
# Phase D0 — lightweight host + data health snapshot (structured, greppable). No monitoring stack
# yet; this is the foundation a watchdog/alerting layer builds on later.
set -euo pipefail
ATP_HOME=/opt/atp
set -a; . "$ATP_HOME/atp.env" 2>/dev/null || true; set +a
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

disk=$(df -h / | awk 'NR==2{print $5" used ("$4" free)"}')
mem=$(free -m | awk 'NR==2{printf "%d/%dMB", $3, $2}')
load=$(cut -d' ' -f1-3 /proc/loadavg)
pg="down"; docker exec atp_postgres pg_isready -U "${PG_SUPERUSER:-postgres}" -d postgres >/dev/null 2>&1 && pg="ok"
redis="down"; docker exec atp_redis redis-cli -a "${REDIS_PASSWORD:-}" ping 2>/dev/null | grep -q PONG && redis="ok"

echo "ts=$now disk=\"$disk\" mem=$mem load=\"$load\" postgres=$pg redis=$redis"
[ "$pg" = ok ] && [ "$redis" = ok ]
