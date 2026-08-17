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


def _migration_009(dialect: str) -> list[str]:
    """AI evaluation & performance tracking (§ Phase G3.1 — read-only, IMMUTABLE history). Two tables:
    ai_predictions (an exact snapshot of the AI consensus at prediction time — NEVER rewritten) and
    ai_prediction_outcomes (the measured result per time horizon — evaluated once, never removed). This
    only EVALUATES predictions; it is not trading logic. No Trading Core / Risk / Broker / IBKR /
    Execution code is touched."""
    t = _types(dialect)
    ts, txt, i, b = t["TS"], t["TXT"], t["INT"], t["BOOL"]
    return [
        f"""CREATE TABLE IF NOT EXISTS ai_predictions (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, timestamp {ts}, score {txt}, direction {txt},
            confidence {txt}, status {txt}, price_at_prediction {txt}, components_snapshot {txt},
            created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_ai_predictions_symbol ON ai_predictions(symbol, timestamp)",
        f"""CREATE TABLE IF NOT EXISTS ai_prediction_outcomes (
            prediction_id {txt} NOT NULL, time_horizon {i} NOT NULL, price_at_prediction {txt},
            future_price {txt}, return_percentage {txt}, direction_correct {b}, evaluated_at {ts} NOT NULL,
            PRIMARY KEY (prediction_id, time_horizon))""",
    ]


def _migration_010(dialect: str) -> list[str]:
    """Outcome Lifecycle Controller (§ Phase G3.2). Additive columns on ai_prediction_outcomes: the
    expected vs actual direction and a per-outcome status — so each evaluated outcome carries a full
    confusion-matrix classification (TRUE/FALSE POSITIVE/NEGATIVE). Outcomes stay immutable (evaluated
    once, never overwritten). Evaluation only — no Trading Core / Risk / Broker / IBKR / Execution."""
    txt = _types(dialect)["TXT"]
    return [
        f"ALTER TABLE ai_prediction_outcomes ADD COLUMN direction_expected {txt}",
        f"ALTER TABLE ai_prediction_outcomes ADD COLUMN direction_actual {txt}",
        f"ALTER TABLE ai_prediction_outcomes ADD COLUMN status {txt}",
    ]


def _migration_011(dialect: str) -> list[str]:
    """AI Decision Governance (§ Phase G3.3 — read-only, IMMUTABLE history). One row per prediction:
    the deterministic governance verdict (APPROVED / PARTIAL / CONFLICT / BLOCKED) with the score,
    confidence, data completeness and reason codes it was based on. Governance decisions are NEVER
    rewritten (ON CONFLICT DO NOTHING). This layer only evaluates decision quality/readiness — it does
    NOT execute trades, generate orders, or touch Trading Core / Risk Engine / Broker / IBKR / Execution."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS ai_governance_results (
            id {txt} PRIMARY KEY, prediction_id {txt} NOT NULL, symbol {txt} NOT NULL,
            status {txt} NOT NULL, score {txt}, confidence {txt}, data_completeness {txt},
            reason_codes {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_ai_governance_symbol ON ai_governance_results(symbol, created_at)",
    ]


def _migration_012(dialect: str) -> list[str]:
    """Data Completeness Engine (§ Phase C1 — read-only reliability layer, IMMUTABLE history). One row per
    symbol/hour: the deterministic 0-100 completeness score across the 7 intelligence domains, the
    readiness state (READY / PARTIAL / INSUFFICIENT), and which sources were available vs missing.
    Snapshots are never rewritten (ON CONFLICT DO NOTHING). This only MEASURES information quality — it
    does NOT trade, generate orders, or touch Trading Core / Risk Engine / Broker / IBKR / Execution."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS data_completeness_snapshots (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, timestamp {ts}, overall_score {txt},
            state {txt}, available_sources {txt}, missing_sources {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_data_completeness_symbol ON data_completeness_snapshots(symbol, timestamp)",
    ]


def _migration_013(dialect: str) -> list[str]:
    """Macro Intelligence Layer (§ Phase R1.2 — read-only, IMMUTABLE history). One row per hour: a
    snapshot of the global macro environment (rates, inflation, employment, volatility, currency,
    commodities). Snapshots are never rewritten (ON CONFLICT DO NOTHING). This is an INTELLIGENCE input
    only — it does NOT trade, generate orders, or touch Trading Core / Risk Engine / Broker / IBKR /
    Execution. Missing metrics stay NULL → NO DATA, never fabricated."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS macro_snapshots (
            id {txt} PRIMARY KEY, timestamp {ts},
            fed_rate {txt}, treasury_10y {txt}, treasury_2y {txt}, cpi {txt}, unemployment {txt},
            vix {txt}, dxy {txt}, oil {txt}, gold {txt}, source {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_macro_snapshots_ts ON macro_snapshots(timestamp)",
    ]


def _migration_014(dialect: str) -> list[str]:
    """Institutional Intelligence Enhancement (§ Phase R1.3 — read-only, IMMUTABLE history). Two tables:
    institutional_position_changes (13F quarter-over-quarter position changes — ACCUMULATION / REDUCTION
    / NEW_POSITION / EXIT) and insider_transactions (SEC Form 4 insider BUY/SELL). Both are append-only
    (ON CONFLICT DO NOTHING → never rewritten). DATA ONLY — no trading, no copy-trading, no order/broker/
    IBKR/execution. Missing data stays NO DATA (never fabricated)."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS institutional_position_changes (
            id {txt} PRIMARY KEY, institution {txt} NOT NULL, symbol {txt} NOT NULL,
            previous_shares {txt}, current_shares {txt}, share_change {txt}, percentage_change {txt},
            direction {txt}, filing_period {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_inst_changes_symbol ON institutional_position_changes(symbol, filing_period)",
        f"""CREATE TABLE IF NOT EXISTS insider_transactions (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, insider_name {txt}, title {txt},
            transaction_type {txt}, shares {txt}, price {txt}, transaction_date {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_insider_symbol ON insider_transactions(symbol, transaction_date)",
    ]


def _migration_015(dialect: str) -> list[str]:
    """Insider Cluster Intelligence (§ Phase R1.4 — read-only, IMMUTABLE history). One row per
    (symbol, time_window) snapshot: the detected insider cluster (ACCUMULATION / DISTRIBUTION / NONE),
    role-weighted score, participant count and aggregate shares/value. Append-only (ON CONFLICT DO
    NOTHING → never rewritten). INTELLIGENCE ONLY — not a trading signal; no order/broker/IBKR/execution.
    Missing Form 4 data → no cluster (NO DATA, never fabricated)."""
    t = _types(dialect)
    ts, txt = t["TS"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS insider_clusters (
            id {txt} PRIMARY KEY, symbol {txt} NOT NULL, time_window {txt} NOT NULL,
            cluster_type {txt}, insider_count {txt}, weighted_score {txt}, total_shares {txt},
            total_value {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_insider_clusters_symbol ON insider_clusters(symbol, time_window)",
    ]


def _migration_016(dialect: str) -> list[str]:
    """Macro core CPI (§ Phase R1.2 fix). The provider already fetches core CPI (CPILFESL, YoY) but it
    had no column, so it was dropped on persist. Additive column — read-only intelligence, no execution."""
    return [f"ALTER TABLE macro_snapshots ADD COLUMN core_cpi {_types(dialect)['TXT']}"]


def _migration_017(dialect: str) -> list[str]:
    """Risk Control Center (§ Phase R2.0 — capital-protection observability + config; NOT trading).
    Two ADDITIVE tables, no change to the canonical `risk_config` (Trading-Core RiskEngine reads that +
    a JSON file — both untouched): `risk_control_policy` holds ONLY the Risk-Control-only fields (no
    duplication of capital / risk_per_trade_pct / max_daily_loss_pct) and references the canonical
    singleton via risk_config_id; `risk_events` is an immutable audit of CONFIGURATION_UPDATED (+ future
    events) with structured details_json. No order/execution/broker table is touched; nothing here can
    create or submit an order."""
    t = _types(dialect)
    ts, txt, i = t["TS"], t["TXT"], t["INT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS risk_control_policy (
            id {txt} PRIMARY KEY, risk_config_id {i} NOT NULL DEFAULT 1, currency {txt},
            warning_threshold_pct {txt}, max_portfolio_exposure_pct {txt}, max_drawdown_pct {txt},
            config_version {i} NOT NULL DEFAULT 0, updated_at {ts}, updated_by {txt})""",
        f"""CREATE TABLE IF NOT EXISTS risk_events (
            id {txt} PRIMARY KEY, timestamp {ts}, event_type {txt} NOT NULL, severity {txt},
            description {txt}, reason_code {txt}, observed_value {txt}, configured_limit {txt},
            configuration_version {txt}, details_json {txt}, created_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_risk_events_ts ON risk_events(created_at)",
    ]


# ---- § Phase R3.0 — Deterministic Backtesting (RESEARCH ONLY; no execution/broker/order) ----
_BT_TERMINAL = "('COMPLETED','FAILED','CANCELLED')"
_BT_CHILD_TABLES = (
    "backtest_decisions", "backtest_trades", "backtest_equity_points", "backtest_metrics", "backtest_events",
)


def _bt_triggers_sqlite() -> list[str]:
    """SQLite BEFORE-triggers that make terminal runs and all their child rows immutable at the DATABASE
    level (not just application code). A run may still transition while non-terminal; child rows are
    insert-only and only while the parent is non-terminal. RAISE(ABORT) rejects any violating write."""
    out = [
        f"""CREATE TRIGGER IF NOT EXISTS trg_bt_runs_no_update_terminal
            BEFORE UPDATE ON backtest_runs WHEN OLD.status IN {_BT_TERMINAL}
            BEGIN SELECT RAISE(ABORT, 'backtest_runs: terminal run is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_bt_runs_no_delete
            BEFORE DELETE ON backtest_runs
            BEGIN SELECT RAISE(ABORT, 'backtest_runs: runs cannot be deleted'); END""",
    ]
    for tbl in _BT_CHILD_TABLES:
        out += [
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_insert_terminal
                BEFORE INSERT ON {tbl}
                WHEN (SELECT status FROM backtest_runs WHERE run_id = NEW.run_id) IN {_BT_TERMINAL}
                BEGIN SELECT RAISE(ABORT, '{tbl}: parent run is terminal (immutable)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_update
                BEFORE UPDATE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows are immutable (insert-only)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_delete
                BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows cannot be deleted'); END""",
        ]
    return out


def _bt_triggers_postgres() -> list[str]:
    """PostgreSQL equivalent: BEFORE-trigger functions enforcing the same terminal immutability."""
    out = [
        """CREATE OR REPLACE FUNCTION atp_bt_block_run_update() RETURNS trigger AS $$
           BEGIN IF OLD.status IN ('COMPLETED','FAILED','CANCELLED') THEN
             RAISE EXCEPTION 'backtest_runs: terminal run is immutable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_bt_block_run_delete() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'backtest_runs: runs cannot be deleted'; END; $$ LANGUAGE plpgsql""",
        # NOTE: the `%%` is a psycopg escape for a LITERAL percent. psycopg parses the query string for
        # placeholders (`%s`/`%b`/`%t`) even with empty params, so a bare `%` here would raise
        # "only '%s','%b','%t' are allowed as placeholders". psycopg un-escapes `%%` → a single `%`,
        # which is exactly the PL/pgSQL RAISE format placeholder for TG_TABLE_NAME. SQLite path is separate
        # and unaffected. See tests/test_migration_postgres_placeholders_r30.py.
        """CREATE OR REPLACE FUNCTION atp_bt_block_child_insert() RETURNS trigger AS $$
           BEGIN IF (SELECT status FROM backtest_runs WHERE run_id = NEW.run_id) IN ('COMPLETED','FAILED','CANCELLED')
             THEN RAISE EXCEPTION '%%: parent run is terminal (immutable)', TG_TABLE_NAME; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_bt_block_child_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_bt_runs_no_update ON backtest_runs",
        "CREATE TRIGGER trg_bt_runs_no_update BEFORE UPDATE ON backtest_runs FOR EACH ROW EXECUTE FUNCTION atp_bt_block_run_update()",
        "DROP TRIGGER IF EXISTS trg_bt_runs_no_delete ON backtest_runs",
        "CREATE TRIGGER trg_bt_runs_no_delete BEFORE DELETE ON backtest_runs FOR EACH ROW EXECUTE FUNCTION atp_bt_block_run_delete()",
    ]
    for tbl in _BT_CHILD_TABLES:
        out += [
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_insert ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_insert BEFORE INSERT ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_bt_block_child_insert()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_upd ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_upd BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_bt_block_child_mutate()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_del ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_del BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_bt_block_child_mutate()",
        ]
    return out


def _migration_018(dialect: str) -> list[str]:
    """§ Phase R3.0 — Deterministic backtesting & strategy validation (RESEARCH ONLY). Six additive,
    immutable-on-terminal tables for internal historical research runs. NOTHING here creates, submits,
    routes, or simulates through a broker/order/execution path — a run only produces internal backtest
    records. Completed/failed/cancelled runs and all their children are frozen by DATABASE triggers
    (see _bt_triggers_*), not by application code alone, plus a deterministic result checksum."""
    t = _types(dialect)
    ts, txt, i, m, b = t["TS"], t["TXT"], t["INT"], t["MONEY"], t["BOOL"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id {txt} PRIMARY KEY, owner {txt} NOT NULL, strategy_id {txt} NOT NULL,
            strategy_version {i} NOT NULL, strategy_config_json {txt} NOT NULL, strategy_checksum {txt} NOT NULL,
            engine_version {txt} NOT NULL, symbol_universe_json {txt} NOT NULL, interval {txt} NOT NULL,
            start_ts {ts} NOT NULL, end_ts {ts} NOT NULL, asset_class {txt} NOT NULL,
            timestamp_policy_id {txt} NOT NULL, timestamp_policy_version {i} NOT NULL,
            exchange_calendar_id {txt} NOT NULL, exchange_calendar_version {txt} NOT NULL,
            exchange_tz {txt} NOT NULL, session_calendar {txt} NOT NULL, data_source {txt} NOT NULL,
            config_snapshot_json {txt} NOT NULL, risk_config_snapshot_json {txt} NOT NULL,
            status {txt} NOT NULL CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
            failure_code {txt}, failure_reason {txt}, warnings_json {txt}, missing_data_json {txt},
            commit_ref {txt}, result_checksum {txt},
            created_at {ts} NOT NULL, started_at {ts}, ended_at {ts}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS backtest_decisions (
            id {txt} PRIMARY KEY, run_id {txt} NOT NULL REFERENCES backtest_runs(run_id),
            seq {i} NOT NULL, ts {ts} NOT NULL, symbol {txt} NOT NULL, strategy_id {txt} NOT NULL,
            strategy_version {i} NOT NULL,
            action {txt} NOT NULL CHECK (action IN ('ENTER_LONG','EXIT','HOLD','NO_DECISION')),
            confidence {txt}, evidence_json {txt}, missing_inputs_json {txt}, reason {txt},
            decision_checksum {txt} NOT NULL, created_at {ts} NOT NULL, UNIQUE (run_id, seq))""",
        f"""CREATE TABLE IF NOT EXISTS backtest_trades (
            id {txt} PRIMARY KEY, run_id {txt} NOT NULL REFERENCES backtest_runs(run_id),
            symbol {txt} NOT NULL, side {txt} NOT NULL CHECK (side = 'LONG'),
            entry_decision_id {txt}, exit_decision_id {txt},
            entry_ts {ts} NOT NULL, entry_fill_ts {ts} NOT NULL, entry_price {m} NOT NULL,
            initial_stop_price {m}, exit_ts {ts}, exit_fill_ts {ts}, exit_price {m},
            quantity {m} NOT NULL CHECK (quantity > 0),
            gross_pnl {m}, commission {m} NOT NULL, slippage {m} NOT NULL, net_pnl {m}, return_pct {txt},
            bars_held {i}, exit_reason {txt} CHECK (exit_reason IS NULL OR exit_reason IN
                ('SIGNAL_EXIT','STOP','TARGET','EOT_LIQUIDATION','AMBIGUOUS_INTRABAR_STOP_FIRST')),
            ambiguous {b} NOT NULL DEFAULT 0, created_at {ts} NOT NULL, UNIQUE (run_id, id))""",
        f"""CREATE TABLE IF NOT EXISTS backtest_equity_points (
            run_id {txt} NOT NULL REFERENCES backtest_runs(run_id), seq {i} NOT NULL, ts {ts} NOT NULL,
            cash {m} NOT NULL, equity {m} NOT NULL, realized_pnl {m} NOT NULL, unrealized_pnl {m} NOT NULL,
            daily_pnl {m}, gross_exposure_pct {txt}, net_exposure_pct {txt}, drawdown_pct {txt},
            PRIMARY KEY (run_id, seq))""",
        f"""CREATE TABLE IF NOT EXISTS backtest_metrics (
            run_id {txt} PRIMARY KEY REFERENCES backtest_runs(run_id),
            metrics_json {txt} NOT NULL, computed_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS backtest_events (
            id {txt} PRIMARY KEY, run_id {txt} NOT NULL REFERENCES backtest_runs(run_id),
            seq {i}, ts {ts}, event_type {txt} NOT NULL, severity {txt}, symbol {txt},
            details_json {txt}, created_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_bt_runs_owner_status ON backtest_runs(owner, status)",
        "CREATE INDEX IF NOT EXISTS ix_bt_runs_created ON backtest_runs(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_bt_decisions_run_ts ON backtest_decisions(run_id, ts)",
        "CREATE INDEX IF NOT EXISTS ix_bt_trades_run ON backtest_trades(run_id, symbol, entry_ts)",
        "CREATE INDEX IF NOT EXISTS ix_bt_equity_run_ts ON backtest_equity_points(run_id, ts)",
        "CREATE INDEX IF NOT EXISTS ix_bt_events_run_type ON backtest_events(run_id, event_type)",
    ]
    triggers = _bt_triggers_postgres() if dialect == "postgres" else _bt_triggers_sqlite()
    return tables + indexes + triggers


def _migration_019(dialect: str) -> list[str]:
    """§ R3.0 hotfix — persist BOTH the expected (decision-derived) and the actual (gap-safe, computed at
    the real fill) risk-per-share on each backtest trade, so a gap-up fill can be shown to never exceed
    the configured risk budget. Additive columns only; no data change."""
    m = _types(dialect)["MONEY"]
    return [
        f"ALTER TABLE backtest_trades ADD COLUMN expected_risk_per_share {m}",
        f"ALTER TABLE backtest_trades ADD COLUMN actual_risk_per_share {m}",
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
    (9, "ai_evaluation", _migration_009),
    (10, "outcome_lifecycle", _migration_010),
    (11, "ai_governance", _migration_011),
    (12, "data_completeness", _migration_012),
    (13, "macro_intelligence", _migration_013),
    (14, "institutional_intelligence", _migration_014),
    (15, "insider_clusters", _migration_015),
    (16, "macro_core_cpi", _migration_016),
    (17, "risk_control_center", _migration_017),
    (18, "research_backtesting", _migration_018),
    (19, "backtest_actual_risk", _migration_019),
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
