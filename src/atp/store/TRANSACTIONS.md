# Phase B — Transaction Boundaries

The durable Store commits safety-critical state in **explicit, short transactions**. Each boundary
below is a single `Store.tx()` block: it commits as a unit or rolls back entirely. PostgreSQL is the
production source of truth; SQLite (WAL, `synchronous=FULL`) is the local/test backend behind the
same interface. Money is exact (`NUMERIC` / canonical decimal `TEXT`), timestamps are ISO-8601 UTC.

| # | Operation | Rows written in one transaction | Crash guarantee |
|---|-----------|--------------------------------|-----------------|
| 1 | **State transition** (`transition`) | `runtime_state` **+** `audit_events` | State never changes without its audit event, and vice-versa |
| 2 | **Kill switch** (`set_kill_switch`) | `kill_switch` **+** `audit_events` | The durable latch flips only with an audit record |
| 3 | **Daily-loss lock** (`set_daily_loss_lock`) | `daily_loss_lock` **+** `audit_events` | The lock is atomic with its reason record |
| 4 | **Fill application** (`apply_fill`) | `fills` **+** `positions` **+** `orders`(→FILLED, broker id) | A fill is never recorded without its position update, nor an order marked filled without its fill — the crash-between-fill-and-position case is impossible |
| 5 | **Order intent** (`insert_order_intent`) | `orders`(INTENT) | Intent is durable before any risk/broker step |
| 6 | **Order state change** (`update_order_state`) | `orders` | Single-row, atomic |
| 7 | **Daily P&L upsert** (`upsert_daily_pnl`) | `daily_pnl` | Today's realized/unrealized are one atomic row |
| 8 | **Risk config / risk state** (`upsert_*`) | `risk_config` / `risk_state` | Singleton rows, atomic |
| 9 | **Decision / heartbeat / md-health** | one row each | Independent atomic writes |
| 10 | **Migration** (`Migrator.apply`) | DDL statements **+** `schema_migrations` | A migration is recorded only if its DDL committed |

## Recovery ordering (read-only, no writes until validated)

On startup the lifecycle **reads** durable state and applies the critical rule *before* any trading:

1. `kill_switch` — engaged ⇒ stay **KILLED** (only manual RESET clears).
2. `runtime_state` — last status was `ARMED/RUNNING/HALTED/KILLED` ⇒ **RECOVERY_REQUIRED**
   (a single `transition` writes the new state + `RECOVERY_START` audit).
3. `run_recovery` runs the fixed 13-step sequence; **all pass ⇒ READY_FOR_ARM** (never RUNNING),
   any fail ⇒ stays RECOVERY_REQUIRED with a `RECOVERY_FAIL` audit.

## Fail-closed (no write, block instead)

`TradingGate.can_trade()` returns **BLOCK** — never opens risk — when: the database does not `ping()`,
the kill switch is engaged, the runtime is not `RUNNING` (incl. `RECOVERY_REQUIRED`), the risk state
cannot be loaded, or the daily-loss lock is engaged. Ambiguity resolves to `RECOVERY_REQUIRED`.

## Idempotency

`orders.idempotency_key` is `UNIQUE`. `OrderManager.place()` returns the existing order for a repeated
key and only submits from state `AUTHORIZED`; the broker submit is itself keyed by `client_order_id`.
An order is therefore never submitted twice across a restart or retry.
