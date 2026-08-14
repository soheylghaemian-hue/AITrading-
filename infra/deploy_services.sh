#!/usr/bin/env bash
# Phase C — deploy the 3 supervised services on the ATP server. Run as root (e.g. via sudo).
# Idempotent. Syncs the repo to origin/main, installs venv deps, migrates atp_prod (owned by the app
# user), installs+starts systemd units running as 'atp'. AUTONOMOUS / PAPER / LIVE stay disabled.
set -euo pipefail
APP=/opt/atp/app
ENVF=/opt/atp/atp.env
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "[deploy] 1/6 sync repo → origin/main"
git config --global --add safe.directory "$APP" 2>/dev/null || true
git -C "$APP" fetch --quiet origin
git -C "$APP" reset --hard origin/main
git -C "$APP" log -1 --oneline

echo "[deploy] 2/6 python deps into venv"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check \
    "psycopg[binary]>=3.1" "redis>=5.0" "fastapi>=0.110" "uvicorn>=0.29" "pytest>=8"

echo "[deploy] 3/6 control token"
if ! grep -q '^ATP_CONTROL_TOKEN=' "$ENVF"; then
    echo "ATP_CONTROL_TOKEN=$(openssl rand -hex 24)" >> "$ENVF"
    echo "  generated ATP_CONTROL_TOKEN"
else
    echo "  ATP_CONTROL_TOKEN present"
fi

echo "[deploy] 4/6 migrate atp_prod (as app user = owner)"
set -a; . "$ENVF"; set +a
PYTHONPATH="$APP/src" "$APP/.venv/bin/python" - <<'PY'
from atp.services.base import build_dsn
from atp.store import open_store
open_store(build_dsn(), migrate=True).close()
print("  migrated:", build_dsn().rsplit("@", 1)[-1])
PY

echo "[deploy] 5/6 ownership → atp"
chown -R atp:atp "$APP"
chown atp:atp "$ENVF"; chmod 600 "$ENVF"

echo "[deploy] 6/6 install + start systemd units"
install -m 644 "$APP/infra/systemd/atp-marketdata.service" /etc/systemd/system/
install -m 644 "$APP/infra/systemd/atp-trading.service"    /etc/systemd/system/
install -m 644 "$APP/infra/systemd/atp-control.service"    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now atp-marketdata atp-trading atp-control
sleep 4
systemctl is-active atp-marketdata atp-trading atp-control
echo "[deploy] DONE"
