"""Schema migrations (§ Phase B).

Versioned, ordered migrations applied by `Migrator` and tracked in `schema_migrations` — the schema is
NEVER created by hand. One DDL template serves both dialects: money is NUMERIC(20,8) in PostgreSQL and
TEXT (canonical decimal) in SQLite; timestamps are ISO-8601 UTC TEXT; booleans are INTEGER 0/1 in both
so the shared SQL layer passes identical parameters.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _types(dialect: str) -> dict:
    money = "NUMERIC(20,8)" if dialect == "postgres" else "TEXT"
    return {"MONEY": money, "TS": "TEXT", "BOOL": "INTEGER", "TXT": "TEXT", "INT": "INTEGER"}


def _statements(dialect: str) -> list[str]:
    t = _types(dialect)
    m, ts, b, txt = t["MONEY"], t["TS"], t["BOOL"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS accounts (
            account_id {txt} PRIMARY KEY, broker {txt} NOT NULL, base_currency {txt} NOT NULL,
            cash {m} NOT NULL, equity {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS runtime_state (
            id {b} PRIMARY KEY, status {txt} NOT NULL, updated_at {ts} NOT NULL,
            correlation_id {txt}, reason {txt})""",
        f"""CREATE TABLE IF NOT EXISTS risk_config (
            id {b} PRIMARY KEY, capital {m} NOT NULL, risk_per_trade_pct {m} NOT NULL,
            max_daily_loss_pct {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS risk_state (
            id {b} PRIMARY KEY, day_start_equity {m} NOT NULL, peak_equity {m} NOT NULL,
            halted {b} NOT NULL DEFAULT 0, killed {b} NOT NULL DEFAULT 0, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS kill_switch (
            id {b} PRIMARY KEY, engaged {b} NOT NULL DEFAULT 0, actor {txt}, reason {txt}, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS daily_pnl (
            trade_date {txt} PRIMARY KEY, day_start_equity {m} NOT NULL, realized_pnl {m} NOT NULL,
            unrealized_pnl {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS daily_loss_lock (
            trade_date {txt} PRIMARY KEY, engaged {b} NOT NULL DEFAULT 0, reason {txt}, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS positions (
            instrument {txt} PRIMARY KEY, quantity {m} NOT NULL, avg_price {m} NOT NULL,
            realized_pnl {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS orders (
            client_order_id {txt} PRIMARY KEY, idempotency_key {txt} NOT NULL UNIQUE, instrument {txt} NOT NULL,
            side {txt} NOT NULL, quantity {m} NOT NULL, order_type {txt} NOT NULL, state {txt} NOT NULL,
            broker_order_id {txt}, correlation_id {txt}, reason {txt},
            created_at {ts} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS fills (
            fill_id {txt} PRIMARY KEY, client_order_id {txt} NOT NULL, instrument {txt} NOT NULL,
            side {txt} NOT NULL, quantity {m} NOT NULL, price {m} NOT NULL, commission {m} NOT NULL,
            ts {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS trades (
            trade_id {txt} PRIMARY KEY, instrument {txt} NOT NULL, direction {txt} NOT NULL,
            quantity {m} NOT NULL, entry_price {m} NOT NULL, exit_price {m}, realized_pnl {m},
            opened_at {ts} NOT NULL, closed_at {ts}, agent {txt}, payload {txt})""",
        f"""CREATE TABLE IF NOT EXISTS decisions (
            decision_id {txt} PRIMARY KEY, ts {ts} NOT NULL, instrument {txt} NOT NULL,
            final_decision {txt}, payload {txt}, correlation_id {txt})""",
        f"""CREATE TABLE IF NOT EXISTS audit_events (
            event_id {txt} PRIMARY KEY, ts {ts} NOT NULL, actor {txt} NOT NULL, action {txt} NOT NULL,
            previous_state {txt}, new_state {txt}, reason {txt}, correlation_id {txt})""",
        f"""CREATE TABLE IF NOT EXISTS service_heartbeats (
            service {txt} PRIMARY KEY, status {txt} NOT NULL, detail {txt}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS market_data_health (
            symbol {txt} PRIMARY KEY, source {txt} NOT NULL, status {txt} NOT NULL,
            latency_ms {t['INT']}, updated_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_orders_state ON orders(state)",
        "CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(client_order_id)",
        "CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_events(ts)",
        "CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts)",
    ]


def _migration_002(dialect: str) -> list[str]:
    """Full authoritative-money coverage: every monetary/risk field is NUMERIC (PG) / decimal TEXT
    (SQLite) — never binary float. Additive, nullable columns."""
    m = _types(dialect)["MONEY"]
    return [
        f"ALTER TABLE orders ADD COLUMN notional {m}",
        f"ALTER TABLE orders ADD COLUMN stop {m}",
        f"ALTER TABLE orders ADD COLUMN target {m}",
        f"ALTER TABLE orders ADD COLUMN monetary_risk {m}",
        f"ALTER TABLE orders ADD COLUMN risk_pct {m}",
        f"ALTER TABLE fills ADD COLUMN slippage {m}",
        f"ALTER TABLE fills ADD COLUMN fees {m}",
        f"ALTER TABLE accounts ADD COLUMN unrealized_pnl {m}",
    ]


def _migration_003(dialect: str) -> list[str]:
    """OHLC bars (§ Phase G1). Durable candle store — the authoritative history the Market Intelligence
    Terminal reads. Composite PK (symbol, interval, ts) is UNIQUE and indexed; a duplicate bar timestamp
    is rejected by the constraint. Money fields are NUMERIC (PG) / decimal TEXT (SQLite)."""
    t = _types(dialect)
    m, ts, txt = t["MONEY"], t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS ohlc_bars (
            symbol {txt} NOT NULL, interval {txt} NOT NULL, ts {ts} NOT NULL,
            open {m} NOT NULL, high {m} NOT NULL, low {m} NOT NULL, close {m} NOT NULL,
            volume {m} NOT NULL, source {txt} NOT NULL, created_at {ts} NOT NULL,
            PRIMARY KEY (symbol, interval, ts))""",
        "CREATE INDEX IF NOT EXISTS ix_ohlc_sym_int_ts ON ohlc_bars(symbol, interval, ts)",
    ]


def _migration_004(dialect: str) -> list[str]:
    """News items (§ Phase G2.1 — news intelligence). Read-only headlines collected by the
    news-intelligence service (real provider text only) with a deterministic sentiment score and
    impact level. `id` is a deterministic hash of (symbol,url) so re-ingesting the same article is
    idempotent (no duplicates). Independent of Trading Core / Risk / Broker / OHLC. Additive."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS news_items (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, title {txt} NOT NULL,
            source {txt}, url {txt}, published_at {ts} NOT NULL, content_summary {txt},
            sentiment_score {txt}, impact_level {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_news_symbol_pub ON news_items(symbol, published_at)",
    ]


def _migration_005(dialect: str) -> list[str]:
    """Trader intelligence (§ Phase G2.5 — read-only). Three additive tables: traders (identity),
    trader_performance (track record — one row per trader), trader_positions (latest position per
    trader+symbol). Float metrics are canonical-decimal TEXT (dialect-agnostic). Quality scores and
    consensus are COMPUTED deterministically on read (never stored, never fabricated). This is an
    intelligence input source only — no Trading Core / Risk / Broker / IBKR / Execution code is touched."""
    t = _types(dialect)
    ts, txt, i = t["TS"], t["TXT"], t["INT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS traders (
            id {txt} PRIMARY KEY, name {txt} NOT NULL, source {txt} NOT NULL, market_focus {txt},
            strategy_type {txt}, track_record_days {i}, created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS trader_performance (
            trader_id {txt} PRIMARY KEY, total_return {txt}, annualized_return {txt}, win_rate {txt},
            max_drawdown {txt}, sharpe_ratio {txt}, sortino_ratio {txt}, average_holding_period {txt},
            number_of_trades {i}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS trader_positions (
            trader_id {txt} NOT NULL, symbol {txt} NOT NULL, direction {txt} NOT NULL,
            entry_price {txt}, position_size {txt}, timestamp {ts} NOT NULL,
            PRIMARY KEY (trader_id, symbol))""",
        "CREATE INDEX IF NOT EXISTS ix_trader_positions_symbol ON trader_positions(symbol)",
    ]


def _migration_006(dialect: str) -> list[str]:
    """Fundamentals intelligence (§ Phase G2.2 — read-only). Four additive tables: companies (profile),
    financial_metrics (latest period per symbol), valuation, analyst_estimates. Numeric fields are
    canonical-decimal TEXT (dialect-agnostic). The company quality score + strengths/risks are COMPUTED
    deterministically on read (never stored, never fabricated). Intelligence input only — no Trading
    Core / Risk / Broker / IBKR / Execution code is touched."""
    t = _types(dialect)
    ts, txt, i = t["TS"], t["TXT"], t["INT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS companies (
            symbol {txt} PRIMARY KEY, company_name {txt}, sector {txt}, industry {txt},
            exchange {txt}, country {txt}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS financial_metrics (
            symbol {txt} PRIMARY KEY, period {txt}, revenue {txt}, revenue_growth {txt},
            gross_margin {txt}, operating_margin {txt}, net_margin {txt}, eps {txt}, eps_growth {txt},
            free_cash_flow {txt}, debt {txt}, cash {txt}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS valuation (
            symbol {txt} PRIMARY KEY, market_cap {txt}, pe_ratio {txt}, forward_pe {txt},
            price_sales {txt}, enterprise_value {txt}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS analyst_estimates (
            symbol {txt} PRIMARY KEY, rating {txt}, target_price {txt}, analyst_count {i},
            upgrade_count {i}, downgrade_count {i}, updated_at {ts} NOT NULL)""",
    ]


def _migration_007(dialect: str) -> list[str]:
    """Options intelligence (§ Phase G2.3 — read-only). Two additive tables: options_snapshot (per
    contract, latest) and options_flow (per-symbol aggregate). Numeric fields are canonical-decimal
    TEXT (dialect-agnostic). The options intelligence score + signals/risks are COMPUTED
    deterministically on read (never stored, never fabricated). Intelligence input only — no Trading
    Core / Risk / Broker / IBKR / Execution code is touched."""
    t = _types(dialect)
    ts, txt, i = t["TS"], t["TXT"], t["INT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS options_snapshot (
            symbol {txt} NOT NULL, expiration_date {txt} NOT NULL, strike {txt} NOT NULL,
            option_type {txt} NOT NULL, timestamp {ts}, bid {txt}, ask {txt}, last {txt},
            volume {i}, open_interest {i}, implied_volatility {txt}, source {txt}, created_at {ts} NOT NULL,
            PRIMARY KEY (symbol, expiration_date, strike, option_type))""",
        "CREATE INDEX IF NOT EXISTS ix_options_snapshot_symbol ON options_snapshot(symbol)",
        f"""CREATE TABLE IF NOT EXISTS options_flow (
            symbol {txt} PRIMARY KEY, timestamp {ts}, call_volume {i}, put_volume {i},
            call_put_ratio {txt}, implied_volatility {txt}, open_interest {i},
            unusual_activity_score {txt}, large_trade_count {i}, premium_volume {txt},
            sentiment {txt}, updated_at {ts} NOT NULL)""",
    ]


def _migration_008(dialect: str) -> list[str]:
    """AI consensus (§ Phase G3 — read-only orchestration). Two additive tables: ai_assessments (the
    per-symbol market view) and ai_assessment_components (the per-source contributions). This is an
    intelligence orchestration snapshot only — NOT a trading decision. The assessment is computed
    deterministically from the other intelligence layers; scores are canonical-decimal TEXT. No Trading
    Core / Risk / Broker / IBKR / Execution code is touched."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS ai_assessments (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, timestamp {ts},
            overall_score {txt}, direction_bias {txt}, confidence {txt}, status {txt}, created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS ai_assessment_components (
            assessment_id {txt} NOT NULL, component_name {txt} NOT NULL, score {txt}, weight {txt},
            direction {txt}, reason {txt}, risk_flags {txt},
            PRIMARY KEY (assessment_id, component_name))""",
    ]


# (version, name, builder) — append new migrations, never edit an applied one.
MIGRATIONS = [
    (1, "initial_schema", _statements),
    (2, "money_columns_numeric", _migration_002),
    (3, "ohlc_bars", _migration_003),
    (4, "news_items", _migration_004),
    (5, "trader_intelligence", _migration_005),
    (6, "fundamentals", _migration_006),
    (7, "options_intelligence", _migration_007),
    (8, "ai_consensus", _migration_008),
]


class Migrator:
    """Applies pending migrations inside transactions; records them in schema_migrations."""

    def __init__(self, store, dialect: str):
        self._store = store
        self._dialect = dialect

    def _applied(self) -> set[int]:
        try:
            rows = self._store._all("SELECT version FROM schema_migrations")
            return {int(r[0]) for r in rows}
        except Exception:
            return set()

    def apply(self) -> list[int]:
        t = _types(self._dialect)
        with self._store.tx() as cur:
            self._store._exec(cur,
                f"CREATE TABLE IF NOT EXISTS schema_migrations (version {t['INT']} PRIMARY KEY, "
                f"name {t['TXT']} NOT NULL, applied_at {t['TS']} NOT NULL)")
        done = self._applied()
        newly: list[int] = []
        for version, name, builder in sorted(MIGRATIONS):
            if version in done:
                continue
            stmts = builder(self._dialect)
            with self._store.tx() as cur:
                for s in stmts:
                    self._store._exec(cur, s)
                self._store._exec(cur,
                    "INSERT INTO schema_migrations (version,name,applied_at) VALUES (?,?,?)",
                    (version, name, datetime.now(timezone.utc).isoformat()))
            newly.append(version)
        return newly
