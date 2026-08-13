# Phase B.5 — Real PostgreSQL Validation Runbook

SQLite proved the persistence *semantics* locally. Before Phase C (service separation) the **exact
same contract** must pass against **real PostgreSQL** — the production source of truth. We never fake
these tests with SQLite; they are skipped unless a real Postgres DSN is provided.

## What the integration suite proves (`tests/integration/test_postgres_contract.py`)

| Area | Test |
|------|------|
| Migrations | versioned migrations `[1, 2]` applied on real PG |
| NUMERIC precision | `information_schema` reports `numeric` for every money column; exact round-trip |
| Transactions / rollback | an error inside a transaction leaves **no** partial fill/position |
| Idempotency (UNIQUE) | two workers, same `idempotency_key` → **exactly one** intent survives |
| Concurrent fills | two fills on one instrument with `SELECT … FOR UPDATE` → no lost update |
| Risk-budget race | two authorizations racing the daily budget → **cannot** jointly exceed it |
| Kill during auth | kill engaged → gate **fails closed** |
| Reconnect | new connection after close → state intact, RUNNING → RECOVERY_REQUIRED |
| Connection failure | DB unavailable → **NO NEW TRADE** |
| Crash recovery | RUNNING→restart→RECOVERY_REQUIRED; KILLED persists; daily P&L −$28k persists; open position restored; pending order recovered **without duplicate submission** |

## Run it (local, disposable Postgres via Docker)

```bash
# 1. bring up an ephemeral Postgres (same engine family as production)
docker compose -f docker-compose.postgres.yml up -d

# 2. install the driver (into your venv; not required for the SQLite suite)
python3 -m pip install "psycopg[binary]>=3.1"

# 3. point the tests at it and run
export ATP_TEST_POSTGRES_DSN="postgresql://atp:atp@localhost:5432/atp_test"
PYTHONPATH=src python3 -m pytest tests/integration -q

# 4. tear down
docker compose -f docker-compose.postgres.yml down -v
```

## Run it without Docker (native Postgres)

```bash
# macOS:  brew install postgresql@16 && brew services start postgresql@16
# Debian: sudo apt-get install -y postgresql && sudo service postgresql start
createuser atp --pwprompt        # password: atp
createdb atp_test -O atp
export ATP_TEST_POSTGRES_DSN="postgresql://atp:atp@localhost:5432/atp_test"
python3 -m pip install "psycopg[binary]>=3.1"
PYTHONPATH=src python3 -m pytest tests/integration -q
```

## Production (Linux VM / container, Phase D)

The production trading host runs managed or containerized PostgreSQL 16 as the durable source of
truth. The application connects with a least-privilege role via `ATP_DATABASE_URL`. Migrations run on
deploy (`Migrator.apply()`); no table is created by hand. Nightly `pg_dump` + WAL/PITR for backups.
Redis, when added, is **bus/cache only** — never the sole copy of positions, orders, fills, risk
state, kill switch, daily P&L, or runtime state.

## Note on this machine

This developer Mac has **no Postgres, no psycopg, and no Docker** installed, so the integration suite
is currently **SKIPPED** here (by design — we do not fake it with SQLite). It runs as soon as any of
the two paths above provides a Postgres DSN.
