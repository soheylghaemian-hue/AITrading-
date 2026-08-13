# Phase D0 — Production Infrastructure Bootstrap (data layer only)

Stand up a persistent 24/7 Linux host with **PostgreSQL 16 + Redis**, hardened, so the Phase B.5
persistence contract can be validated against **real PostgreSQL**. **No trading services** are
installed here (no IB Gateway, IBC, Xvfb, Massive, Trading Core, Autonomous, Execution).

`AUTONOMOUS = DISABLED · LIVE EXECUTION = DISABLED · IBKR ORDERS = 0` — this phase changes none of that.

---

## What I cannot do (needs YOU)

Provisioning a server requires a cloud account, a payment method, and SSH setup — I must not request
or handle passwords or private keys. So **you** create the VM; **I** provide the one-command
bootstrap and the validation it runs. Do the steps below; paste the validation output back and I
verify it.

---

## Step 1 — Provision the VM (you)

- **Provider:** Hetzner Cloud, AWS Lightsail/EC2, DigitalOcean, or similar.
- **Image:** Ubuntu **24.04 LTS**.
- **Size:** ≥ **4 vCPU / 8 GB RAM / 80 GB SSD**, **US-East** region, **static public IP**.
- **SSH:** add **your** public key at creation (key auth only). Note the admin username (e.g. `ubuntu`).

You now have `ssh ubuntu@<IP>`.

## Step 2 — Bootstrap (you, on the VM)

```bash
sudo apt-get update && sudo apt-get install -y git
sudo install -d -o "$USER" /opt/atp
git clone https://github.com/soheylghaemian-hue/AITrading-.git /opt/atp/app
cd /opt/atp/app && git checkout main          # commit 6dfb8c7 or later approved main
sudo REPO_DIR=/opt/atp/app ./infra/bootstrap.sh
```

`bootstrap.sh` installs Docker + Compose, UFW firewall, unattended security updates, UTC time sync,
creates the non-root `atp` service user, **generates strong secrets** into `/opt/atp/atp.env`
(chmod 600), and starts PostgreSQL + Redis (loopback-only, private).

## Step 3 — Verify the data stack (you)

```bash
docker compose --env-file /opt/atp/atp.env -f /opt/atp/app/infra/docker-compose.data.yml ps
sudo /opt/atp/app/infra/healthcheck.sh          # → postgres=ok redis=ok
sudo ufw status verbose                         # SSH + 443 only; 5432/6379 NOT public
```

## Step 4 — SSH hardening (you, AFTER key login is confirmed)

Only once you can log in with your key, disable passwords + root login:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

---

## Step 5 — Phase B.5 validation against REAL PostgreSQL (the point of D0)

```bash
cd /opt/atp/app
python3 -m venv .venv && . .venv/bin/activate
pip install -q pytest "psycopg[binary]>=3.1"

# build the TEST DSN from the generated secrets (test DB only)
set -a; . /opt/atp/atp.env; set +a
export ATP_TEST_POSTGRES_DSN="postgresql://${ATP_TEST_USER}:${ATP_TEST_PASSWORD}@127.0.0.1:5432/${ATP_TEST_DB}"

PYTHONPATH=src python3 -m pytest tests/integration -q
```

This proves, on real PostgreSQL: migrations, NUMERIC precision (via `information_schema`),
transactions, rollback, UNIQUE idempotency race, concurrent fills, risk-vs-daily-loss race,
kill-switch race, crash recovery (KILLED / daily-P&L / open position / pending order), DB
disconnect fail-closed, and reconnect-without-auto-RUNNING. **Never** run this on SQLite.

## Step 6 — Failure test (you)

```bash
# stop the database → the gate must fail closed (NO NEW TRADE)
docker compose --env-file /opt/atp/atp.env -f infra/docker-compose.data.yml stop postgres
PYTHONPATH=src python3 -c "from atp.store import open_store; \
  import os; s=open_store(os.environ['ATP_TEST_POSTGRES_DSN'], migrate=False); print('ping', s.ping())"
#   → connection error / ping False  ⇒ callers BLOCK (see tests/integration DB-failure cases)

# restart the database → the system does NOT auto-resume RUNNING (recovery → RECOVERY_REQUIRED)
docker compose --env-file /opt/atp/atp.env -f infra/docker-compose.data.yml start postgres
```

Paste the pytest summary + failure-test output back to me.

---

## Security invariants (enforced above)

- No application runs as root; `atp` is a dedicated system user.
- PostgreSQL and Redis bind to **127.0.0.1 only** and UFW blocks the outside — 5432 / 6379 /
  4001 / 4002 are never public.
- Secrets live only in `/opt/atp/atp.env` (chmod 600) — never in Git, the frontend, `NEXT_PUBLIC_*`,
  or logs. Separate prod / test / redis credentials.
- **PostgreSQL is the source of truth.** Redis is bus/cache only — never the sole copy of positions,
  orders, fills, risk state, kill switch, daily P&L, or runtime state.
