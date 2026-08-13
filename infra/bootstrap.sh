#!/usr/bin/env bash
# Phase D0 — production infrastructure bootstrap for a FRESH Ubuntu 24.04 LTS host.
# Installs ONLY the data layer + host hardening. NO trading services, NO IB Gateway, NO Massive.
#
#   sudo ./bootstrap.sh
#
# Idempotent where practical. Generates strong secrets into /opt/atp/atp.env on first run.
# After this completes, PostgreSQL + Redis run 24/7 (private, loopback-only) and the Phase B.5
# integration suite can be run against real PostgreSQL (see infra/README.md).
set -euo pipefail

ATP_HOME=/opt/atp
ATP_USER=atp
ENV_FILE="$ATP_HOME/atp.env"
REPO_DIR="${REPO_DIR:-$ATP_HOME/app}"     # where the ATP repo is cloned
COMPOSE="$ATP_HOME/app/infra/docker-compose.data.yml"

log(){ printf '\n\033[1;36m[D0]\033[0m %s\n' "$*"; }
require_root(){ [ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }; }

require_root
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "WARN: tested on Ubuntu 24.04; detected ${PRETTY_NAME:-unknown}"

# ---------------------------------------------------------------- 1. time + timezone (UTC)
log "time synchronization + UTC"
timedatectl set-timezone UTC || true
timedatectl set-ntp true || true

# ---------------------------------------------------------------- 2. base packages
log "apt update + base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades chrony jq git python3-venv python3-pip

# ---------------------------------------------------------------- 3. automatic security updates
log "unattended-upgrades (security)"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades || true

# ---------------------------------------------------------------- 4. Docker Engine + Compose plugin
if ! command -v docker >/dev/null 2>&1; then
  log "install Docker Engine + Compose plugin (official repo)"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" >/etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker --version; docker compose version

# ---------------------------------------------------------------- 5. non-root service user
if ! id "$ATP_USER" >/dev/null 2>&1; then
  log "create non-root service user '$ATP_USER'"
  adduser --system --group --home "$ATP_HOME" "$ATP_USER"
fi
usermod -aG docker "$ATP_USER" || true
# the admin login user (the one you SSH in as) also gets docker access for ops
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then usermod -aG docker "$SUDO_USER" || true; fi
install -d -o "$ATP_USER" -g "$ATP_USER" -m 0750 "$ATP_HOME" "$ATP_HOME/backups"

# ---------------------------------------------------------------- 6. secrets (server-side, generated)
if [ ! -f "$ENV_FILE" ]; then
  log "generate server-side secrets → $ENV_FILE (chmod 600)"
  gen(){ openssl rand -hex 24; }
  cat >"$ENV_FILE" <<EOF
PG_SUPERUSER=postgres
PG_SUPERUSER_PASSWORD=$(gen)
ATP_PROD_DB=atp_prod
ATP_APP_USER=atp_app
ATP_APP_PASSWORD=$(gen)
ATP_TEST_DB=atp_test
ATP_TEST_USER=atp_test
ATP_TEST_PASSWORD=$(gen)
REDIS_PASSWORD=$(gen)
EOF
  chown "$ATP_USER:$ATP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
else
  log "env file exists — keeping existing secrets"
fi

# ---------------------------------------------------------------- 7. firewall (ufw)
log "host firewall: allow SSH + 443 (future HTTPS); deny everything else inbound"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 443/tcp
# 5432 (Postgres), 6379 (Redis), 4001/4002 (IBKR) are NEVER opened — they bind to loopback only.
ufw --force enable
ufw status verbose || true

# ---------------------------------------------------------------- 8. bring up the data stack
if [ -f "$COMPOSE" ]; then
  log "start PostgreSQL + Redis (private, loopback-only)"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up -d
else
  log "NOTE: repo not found at $REPO_DIR — clone it there, then run:"
  echo "  docker compose --env-file $ENV_FILE -f $REPO_DIR/infra/docker-compose.data.yml up -d"
fi

# ---------------------------------------------------------------- 9. nightly backups (cron)
log "install nightly PostgreSQL backup cron"
install -m 0755 "$REPO_DIR/infra/backup.sh" /usr/local/bin/atp-backup 2>/dev/null || true
cat >/etc/cron.d/atp-backup <<EOF
# nightly pg_dump of the production database (retain 14 days)
30 2 * * * root /usr/local/bin/atp-backup >> /var/log/atp-backup.log 2>&1
EOF

log "DONE. Verify:  docker compose --env-file $ENV_FILE -f $COMPOSE ps"
echo "SSH hardening (do AFTER confirming key login works): see infra/README.md §SSH."
