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


# ---- § Phase R3.0A — immutable versioned research OHLC datasets (RESEARCH ONLY) ----
_RD_TERMINAL = "('COMPLETED','FAILED')"
_RD_CHILD_TABLES = ("research_ohlc_bars", "research_dataset_events")


def _rd_triggers_sqlite() -> list[str]:
    out = [
        f"""CREATE TRIGGER IF NOT EXISTS trg_research_datasets_no_update_terminal
            BEFORE UPDATE ON research_datasets WHEN OLD.status IN {_RD_TERMINAL}
            BEGIN SELECT RAISE(ABORT, 'research_datasets: terminal dataset is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_research_datasets_no_delete
            BEFORE DELETE ON research_datasets
            BEGIN SELECT RAISE(ABORT, 'research_datasets: datasets cannot be deleted'); END""",
    ]
    for tbl in _RD_CHILD_TABLES:
        out += [
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_insert_terminal
                BEFORE INSERT ON {tbl}
                WHEN (SELECT status FROM research_datasets WHERE dataset_id = NEW.dataset_id) IN {_RD_TERMINAL}
                BEGIN SELECT RAISE(ABORT, '{tbl}: parent dataset is terminal (immutable)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_update
                BEFORE UPDATE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows are immutable (insert-only)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_delete
                BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows cannot be deleted'); END""",
        ]
    return out


def _rd_triggers_postgres() -> list[str]:
    # `%%` is the psycopg literal-percent escape → PL/pgSQL receives a single `%` (the RAISE placeholder).
    out = [
        """CREATE OR REPLACE FUNCTION atp_rd_block_ds_update() RETURNS trigger AS $$
           BEGIN IF OLD.status IN ('COMPLETED','FAILED') THEN
             RAISE EXCEPTION 'research_datasets: terminal dataset is immutable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_rd_block_ds_delete() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'research_datasets: datasets cannot be deleted'; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_rd_block_child_insert() RETURNS trigger AS $$
           BEGIN IF (SELECT status FROM research_datasets WHERE dataset_id = NEW.dataset_id) IN ('COMPLETED','FAILED')
             THEN RAISE EXCEPTION '%%: parent dataset is terminal (immutable)', TG_TABLE_NAME; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_rd_block_child_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_research_datasets_no_update ON research_datasets",
        "CREATE TRIGGER trg_research_datasets_no_update BEFORE UPDATE ON research_datasets FOR EACH ROW EXECUTE FUNCTION atp_rd_block_ds_update()",
        "DROP TRIGGER IF EXISTS trg_research_datasets_no_delete ON research_datasets",
        "CREATE TRIGGER trg_research_datasets_no_delete BEFORE DELETE ON research_datasets FOR EACH ROW EXECUTE FUNCTION atp_rd_block_ds_delete()",
    ]
    for tbl in _RD_CHILD_TABLES:
        out += [
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_insert ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_insert BEFORE INSERT ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_rd_block_child_insert()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_upd ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_upd BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_rd_block_child_mutate()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_del ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_del BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_rd_block_child_mutate()",
        ]
    return out


def _migration_020(dialect: str) -> list[str]:
    """§ Phase R3.0A — immutable, versioned research OHLC datasets built from historical 1-minute
    aggregates, RTH-normalized to regular-session daily bars. NEVER overwrites the live `ohlc_bars`
    (separate tables). A dataset is idempotent by `request_checksum`; COMPLETED/FAILED are terminal and
    frozen by DATABASE triggers. A replacement dataset carries `supersedes_dataset_id` (the old dataset is
    never mutated — 'superseded' is derived in the read model, there is no SUPERSEDED status). Also adds
    NULLABLE dataset-pinning columns to backtest_runs (NULL for legacy/fixture runs; terminal rows are
    never updated)."""
    t = _types(dialect)
    ts, txt, i, m, b = t["TS"], t["TXT"], t["INT"], t["MONEY"], t["BOOL"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS research_datasets (
            dataset_id {txt} PRIMARY KEY, owner {txt} NOT NULL, request_checksum {txt} NOT NULL,
            supersedes_dataset_id {txt}, retry_of_dataset_id {txt},
            symbol_universe_json {txt} NOT NULL, interval {txt} NOT NULL, provider {txt} NOT NULL,
            provider_contract_version {txt} NOT NULL, adjustment_policy {txt} NOT NULL,
            normalization_policy {txt} NOT NULL, calendar_version {txt} NOT NULL,
            range_start {ts} NOT NULL, range_end {ts} NOT NULL,
            status {txt} NOT NULL CHECK (status IN ('PLANNED','RUNNING','COMPLETED','FAILED')),
            row_count {i}, missing_minute_threshold {txt}, raw_pages_checksum {txt}, dataset_checksum {txt},
            provider_adjusted_flag {b}, warnings_json {txt}, missing_data_json {txt},
            failure_code {txt}, failure_reason {txt},
            created_at {ts} NOT NULL, started_at {ts}, ended_at {ts}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS research_ohlc_bars (
            dataset_id {txt} NOT NULL REFERENCES research_datasets(dataset_id),
            symbol {txt} NOT NULL, interval {txt} NOT NULL, ts {ts} NOT NULL, session_date {txt} NOT NULL,
            open {m} NOT NULL, high {m} NOT NULL, low {m} NOT NULL, close {m} NOT NULL, volume {m} NOT NULL,
            trade_count {i}, source {txt} NOT NULL, adjustment_policy {txt} NOT NULL, created_at {ts} NOT NULL,
            PRIMARY KEY (dataset_id, symbol, interval, ts))""",
        f"""CREATE TABLE IF NOT EXISTS research_dataset_events (
            id {txt} PRIMARY KEY, dataset_id {txt} NOT NULL REFERENCES research_datasets(dataset_id),
            seq {i}, ts {ts}, event_type {txt} NOT NULL, severity {txt}, symbol {txt}, details_json {txt},
            created_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_research_datasets_req ON research_datasets(request_checksum, status)",
        "CREATE INDEX IF NOT EXISTS ix_research_datasets_owner ON research_datasets(owner, status)",
        "CREATE INDEX IF NOT EXISTS ix_research_bars_ds ON research_ohlc_bars(dataset_id, symbol, interval, ts)",
        "CREATE INDEX IF NOT EXISTS ix_research_events_ds ON research_dataset_events(dataset_id, event_type)",
    ]
    pins = [
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_id {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_provider {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_provider_contract_version {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_adjustment_policy {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_normalization_policy {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_calendar_version {txt}",
        f"ALTER TABLE backtest_runs ADD COLUMN dataset_checksum {txt}",
    ]
    triggers = _rd_triggers_postgres() if dialect == "postgres" else _rd_triggers_sqlite()
    return tables + indexes + pins + triggers


# --------------------------------------------------------------------------- § R3.1A immutable triggers
# Insert-only, write-once tables (snapshots/inputs/outcomes/events/metrics): ANY update or delete is
# rejected. `research_validation_runs` is status-terminal (RUNNING → COMPLETED|FAILED|INSUFFICIENT): a
# terminal run is frozen, and no run may be deleted. Supersede-not-mutate: a corrected snapshot is a NEW
# row referencing the old via `supersedes_snapshot_id`; the original is never changed.
_RI_IMMUTABLE_TABLES = ("research_intel_snapshots", "research_intel_snapshot_inputs",
                        "research_intel_outcomes", "research_intel_collection_events",
                        "research_validation_metrics")
_RV_TERMINAL = "('COMPLETED','FAILED','INSUFFICIENT')"


def _ri_triggers_sqlite() -> list[str]:
    out: list[str] = []
    for tbl in _RI_IMMUTABLE_TABLES:
        out += [
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_update BEFORE UPDATE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows are immutable (insert-only)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_delete BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows cannot be deleted'); END""",
        ]
    out += [
        f"""CREATE TRIGGER IF NOT EXISTS trg_rv_runs_no_update_terminal
            BEFORE UPDATE ON research_validation_runs WHEN OLD.status IN {_RV_TERMINAL}
            BEGIN SELECT RAISE(ABORT, 'research_validation_runs: terminal run is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_rv_runs_no_delete
            BEFORE DELETE ON research_validation_runs
            BEGIN SELECT RAISE(ABORT, 'research_validation_runs: runs cannot be deleted'); END""",
    ]
    return out


def _ri_triggers_postgres() -> list[str]:
    # `%%` is the psycopg literal-percent escape → PL/pgSQL receives a single `%` (the RAISE placeholder).
    out = [
        """CREATE OR REPLACE FUNCTION atp_ri_block_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_rv_block_run_update() RETURNS trigger AS $$
           BEGIN IF OLD.status IN ('COMPLETED','FAILED','INSUFFICIENT') THEN
             RAISE EXCEPTION 'research_validation_runs: terminal run is immutable'; END IF;
             RETURN NEW; END; $$ LANGUAGE plpgsql""",
    ]
    for tbl in _RI_IMMUTABLE_TABLES:
        out += [
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_upd ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_upd BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_ri_block_mutate()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_del ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_del BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_ri_block_mutate()",
        ]
    out += [
        "DROP TRIGGER IF EXISTS trg_rv_runs_no_update ON research_validation_runs",
        "CREATE TRIGGER trg_rv_runs_no_update BEFORE UPDATE ON research_validation_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_rv_block_run_update()",
        "DROP TRIGGER IF EXISTS trg_rv_runs_no_delete ON research_validation_runs",
        "CREATE TRIGGER trg_rv_runs_no_delete BEFORE DELETE ON research_validation_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_ri_block_mutate()",
    ]
    return out


def _migration_021(dialect: str) -> list[str]:
    """§ Phase R3.1A — immutable point-in-time AI/consensus intelligence collection + validation substrate.
    RESEARCH DATA ONLY: no trading, no orders, no execution, never touches live `ohlc_bars`. Forward-only
    snapshots (one per pilot symbol per completed NYSE session) with a canonical input envelope + provenance;
    outcomes are pinned to a COMPLETED immutable research dataset ONLY after a horizon matures. All six tables
    are DATABASE-immutable (insert-only, except validation_runs which is status-terminal). Additive; never
    touches migrations 18/19/20 or live tables."""
    t = _types(dialect)
    ts, txt, i, b = t["TS"], t["TXT"], t["INT"], t["BOOL"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS research_intel_snapshots (
            snapshot_id {txt} PRIMARY KEY, universe_id {txt} NOT NULL, universe_version {txt} NOT NULL,
            sampling_policy_version {txt} NOT NULL, outcome_policy_version {txt} NOT NULL,
            symbol {txt} NOT NULL, asset_class {txt} NOT NULL, exchange {txt} NOT NULL, currency {txt} NOT NULL,
            exchange_tz {txt} NOT NULL, calendar_id {txt} NOT NULL, calendar_version {txt} NOT NULL,
            scheduled_target_ts {ts} NOT NULL, computation_started_ts {ts} NOT NULL,
            decision_ts {ts} NOT NULL, decision_session_date {txt} NOT NULL, is_early_close {b},
            decision_price {txt}, decision_price_source {txt}, decision_price_provenance_status {txt},
            decision_price_bar_ts {txt},
            consensus_score {txt}, consensus_direction {txt}, consensus_confidence {txt}, consensus_status {txt},
            governance_status {txt}, governance_reasons_json {txt}, data_completeness {txt},
            expected_outcome_contract_json {txt} NOT NULL, adjustment_policy {txt} NOT NULL,
            horizons_json {txt} NOT NULL, inputs_checksum {txt} NOT NULL, snapshot_checksum {txt} NOT NULL,
            commit_sha {txt} NOT NULL, supersedes_snapshot_id {txt}, status {txt} NOT NULL,
            created_at {ts} NOT NULL,
            UNIQUE (symbol, calendar_version, decision_session_date, sampling_policy_version))""",
        f"""CREATE TABLE IF NOT EXISTS research_intel_snapshot_inputs (
            snapshot_id {txt} NOT NULL REFERENCES research_intel_snapshots(snapshot_id),
            component_name {txt} NOT NULL, canonical_value_json {txt}, component_score {txt},
            component_status {txt}, source_provider {txt}, source_event_ts {ts},
            source_published_or_filed_ts {ts}, source_observed_ts {ts}, source_available_ts {ts},
            provenance_status {txt} NOT NULL CHECK (provenance_status IN ('VERIFIED','OBSERVED_ONLY','UNKNOWN')),
            missing_data_reason {txt}, freshness_state {txt}, created_at {ts} NOT NULL,
            PRIMARY KEY (snapshot_id, component_name))""",
        f"""CREATE TABLE IF NOT EXISTS research_intel_outcomes (
            snapshot_id {txt} NOT NULL REFERENCES research_intel_snapshots(snapshot_id),
            horizon_sessions {i} NOT NULL, snapshot_checksum {txt} NOT NULL,
            dataset_id {txt} REFERENCES research_datasets(dataset_id), dataset_checksum {txt},
            provider_contract_version {txt}, adjustment_policy {txt},
            decision_bar_ts {ts}, decision_price {txt}, outcome_bar_ts {ts}, outcome_price {txt},
            return_pct {txt}, direction_expected {txt}, direction_actual {txt}, direction_correct {b},
            classification {txt}, neutral_threshold_pct {txt}, outcome_policy_version {txt} NOT NULL,
            decision_price_bar_ts {txt}, decision_price_reconciliation {txt}, outcome_checksum {txt},
            status {txt} NOT NULL CHECK (status IN ('MATURED','FAILED')), failure_code {txt},
            evaluation_ts {ts} NOT NULL, commit_sha {txt} NOT NULL, created_at {ts} NOT NULL,
            PRIMARY KEY (snapshot_id, horizon_sessions))""",
        f"""CREATE TABLE IF NOT EXISTS research_intel_collection_events (
            id {txt} PRIMARY KEY, snapshot_id {txt} REFERENCES research_intel_snapshots(snapshot_id),
            event_type {txt} NOT NULL, severity {txt}, ts {ts}, symbol {txt}, session_date {txt},
            details_json {txt}, commit_sha {txt}, created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS research_validation_runs (
            run_id {txt} PRIMARY KEY, universe_id {txt} NOT NULL, universe_version {txt} NOT NULL,
            validation_policy_version {txt} NOT NULL, outcome_policy_version {txt} NOT NULL,
            sampling_policy_version {txt} NOT NULL, gate_id {txt} NOT NULL,
            snapshot_set_checksum {txt}, outcome_set_checksum {txt}, dataset_ids_json {txt},
            commit_sha {txt} NOT NULL, result_checksum {txt},
            status {txt} NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED','INSUFFICIENT')),
            gate_report_json {txt}, created_at {ts} NOT NULL, started_at {ts}, ended_at {ts},
            updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS research_validation_metrics (
            run_id {txt} NOT NULL REFERENCES research_validation_runs(run_id),
            metric_group {txt} NOT NULL, metrics_json {txt}, created_at {ts} NOT NULL,
            PRIMARY KEY (run_id, metric_group))""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_ri_snap_symbol ON research_intel_snapshots(symbol, decision_session_date)",
        "CREATE INDEX IF NOT EXISTS ix_ri_snap_universe ON research_intel_snapshots(universe_id, decision_session_date)",
        "CREATE INDEX IF NOT EXISTS ix_ri_out_status ON research_intel_outcomes(status, horizon_sessions)",
        "CREATE INDEX IF NOT EXISTS ix_ri_events_snap ON research_intel_collection_events(snapshot_id, event_type)",
        "CREATE INDEX IF NOT EXISTS ix_rv_runs_status ON research_validation_runs(status, created_at)",
    ]
    triggers = _ri_triggers_postgres() if dialect == "postgres" else _ri_triggers_sqlite()
    return tables + indexes + triggers


# ---- § Phase P2 — durable Paper Canary ledger (PAPER ONLY) -------------------------------
def _paper_canary_triggers_sqlite() -> list[str]:
    """Enforce append-only fills/events and the Paper Canary order state machine in SQLite."""
    return [
        """CREATE TRIGGER IF NOT EXISTS trg_paper_order_events_no_update
            BEFORE UPDATE ON paper_order_events
            BEGIN SELECT RAISE(ABORT, 'paper_order_events: rows are immutable (insert-only)'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_order_events_no_delete
            BEFORE DELETE ON paper_order_events
            BEGIN SELECT RAISE(ABORT, 'paper_order_events: rows cannot be deleted'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_fills_no_update
            BEFORE UPDATE ON paper_fills
            BEGIN SELECT RAISE(ABORT, 'paper_fills: rows are immutable (insert-only)'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_fills_no_delete
            BEFORE DELETE ON paper_fills
            BEGIN SELECT RAISE(ABORT, 'paper_fills: rows cannot be deleted'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_no_delete
            BEFORE DELETE ON paper_orders
            BEGIN SELECT RAISE(ABORT, 'paper_orders: rows cannot be deleted'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_terminal_immutable
            BEFORE UPDATE ON paper_orders
            WHEN OLD.state IN ('FILLED','REJECTED','CANCELLED')
            BEGIN SELECT RAISE(ABORT, 'paper_orders: terminal order is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_request_immutable
            BEFORE UPDATE ON paper_orders
            WHEN OLD.client_order_id IS NOT NEW.client_order_id
              OR OLD.run_id IS NOT NEW.run_id
              OR OLD.idempotency_key IS NOT NEW.idempotency_key
              OR OLD.decision_id IS NOT NEW.decision_id
              OR OLD.instrument IS NOT NEW.instrument
              OR OLD.side IS NOT NEW.side
              OR OLD.quantity IS NOT NEW.quantity
              OR OLD.order_type IS NOT NEW.order_type
              OR OLD.request_checksum IS NOT NEW.request_checksum
              OR OLD.risk_config_checksum IS NOT NEW.risk_config_checksum
              OR OLD.quote_bid IS NOT NEW.quote_bid
              OR OLD.quote_ask IS NOT NEW.quote_ask
              OR OLD.quote_ts IS NOT NEW.quote_ts
              OR OLD.correlation_id IS NOT NEW.correlation_id
              OR OLD.created_at IS NOT NEW.created_at
            BEGIN SELECT RAISE(ABORT, 'paper_orders: request fields are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_state_graph
            BEFORE UPDATE ON paper_orders
            WHEN NEW.version != OLD.version + 1 OR (
              OLD.state IS NOT NEW.state AND NOT (
                (OLD.state = 'INTENT' AND NEW.state IN ('AUTHORIZED','REJECTED','CANCELLED'))
                OR (OLD.state = 'AUTHORIZED' AND NEW.state IN ('FILLED','REJECTED','CANCELLED'))))
            BEGIN SELECT RAISE(ABORT, 'paper_orders: invalid state transition'); END""",
    ]


def _paper_canary_triggers_postgres() -> list[str]:
    """PostgreSQL equivalent of the durable Paper Canary write boundary."""
    return [
        # `%%` is the psycopg literal-percent escape; PL/pgSQL receives `%` for RAISE formatting.
        """CREATE OR REPLACE FUNCTION atp_paper_order_event_block_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_paper_order_events_no_update ON paper_order_events",
        "CREATE TRIGGER trg_paper_order_events_no_update BEFORE UPDATE ON paper_order_events "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_order_event_block_mutate()",
        "DROP TRIGGER IF EXISTS trg_paper_order_events_no_delete ON paper_order_events",
        "CREATE TRIGGER trg_paper_order_events_no_delete BEFORE DELETE ON paper_order_events "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_order_event_block_mutate()",
        """CREATE OR REPLACE FUNCTION atp_paper_block_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'Paper Canary ledger row is immutable'; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_paper_fills_no_update ON paper_fills",
        "CREATE TRIGGER trg_paper_fills_no_update BEFORE UPDATE ON paper_fills "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_block_mutate()",
        "DROP TRIGGER IF EXISTS trg_paper_fills_no_delete ON paper_fills",
        "CREATE TRIGGER trg_paper_fills_no_delete BEFORE DELETE ON paper_fills "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_block_mutate()",
        "DROP TRIGGER IF EXISTS trg_paper_orders_no_delete ON paper_orders",
        "CREATE TRIGGER trg_paper_orders_no_delete BEFORE DELETE ON paper_orders "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_block_mutate()",
        """CREATE OR REPLACE FUNCTION atp_paper_validate_order_update() RETURNS trigger AS $$
           BEGIN
             IF OLD.state IN ('FILLED','REJECTED','CANCELLED') THEN
               RAISE EXCEPTION 'paper_orders: terminal order is immutable';
             END IF;
             IF OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
                OR OLD.run_id IS DISTINCT FROM NEW.run_id
                OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
                OR OLD.decision_id IS DISTINCT FROM NEW.decision_id
                OR OLD.instrument IS DISTINCT FROM NEW.instrument
                OR OLD.side IS DISTINCT FROM NEW.side
                OR OLD.quantity IS DISTINCT FROM NEW.quantity
                OR OLD.order_type IS DISTINCT FROM NEW.order_type
                OR OLD.request_checksum IS DISTINCT FROM NEW.request_checksum
                OR OLD.risk_config_checksum IS DISTINCT FROM NEW.risk_config_checksum
                OR OLD.quote_bid IS DISTINCT FROM NEW.quote_bid
                OR OLD.quote_ask IS DISTINCT FROM NEW.quote_ask
                OR OLD.quote_ts IS DISTINCT FROM NEW.quote_ts
                OR OLD.correlation_id IS DISTINCT FROM NEW.correlation_id
                OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
               RAISE EXCEPTION 'paper_orders: request fields are immutable';
             END IF;
             IF NEW.version <> OLD.version + 1 OR (
                  OLD.state IS DISTINCT FROM NEW.state AND NOT (
                    (OLD.state = 'INTENT' AND NEW.state IN ('AUTHORIZED','REJECTED','CANCELLED'))
                    OR (OLD.state = 'AUTHORIZED' AND NEW.state IN ('FILLED','REJECTED','CANCELLED')))) THEN
               RAISE EXCEPTION 'paper_orders: invalid state transition';
             END IF;
             RETURN NEW;
           END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_paper_orders_validate_update ON paper_orders",
        "CREATE TRIGGER trg_paper_orders_validate_update BEFORE UPDATE ON paper_orders "
        "FOR EACH ROW EXECUTE FUNCTION atp_paper_validate_order_update()",
    ]


def _migration_022(dialect: str) -> list[str]:
    """§ Phase P2 — durable Paper Canary lifecycle and ledger (PAPER ONLY).

    Dedicated tables deliberately leave the legacy Phase-B account/order/fill/position contract
    untouched. PostgreSQL remains authoritative; SQLite mirrors the schema for deterministic local
    tests. Money uses NUMERIC(20,8) in PostgreSQL and canonical-decimal TEXT in SQLite. The event
    stream is database-append-only, and `active_slot` permits at most one active canary run.
    """
    t = _types(dialect)
    ts, txt, i, m = t["TS"], t["TXT"], t["INT"], t["MONEY"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS paper_canary_runs (
            run_id {txt} PRIMARY KEY,
            status {txt} NOT NULL CHECK (status IN
                ('CREATED','RUNNING','RECOVERY_REQUIRED','READY_FOR_ARM','STOPPED','FAILED','COMPLETED')),
            active_slot {i} UNIQUE CHECK (active_slot IS NULL OR active_slot = 1),
            version {i} NOT NULL DEFAULT 0,
            config_json {txt} NOT NULL, config_checksum {txt} NOT NULL,
            risk_config_checksum {txt} NOT NULL, commit_sha {txt} NOT NULL, reason {txt},
            created_at {ts} NOT NULL, started_at {ts}, heartbeat_at {ts}, ended_at {ts},
            updated_at {ts} NOT NULL,
            CHECK ((status IN ('CREATED','RUNNING','RECOVERY_REQUIRED','READY_FOR_ARM')
                    AND active_slot IS NOT NULL AND active_slot = 1)
                OR (status IN ('STOPPED','FAILED','COMPLETED') AND active_slot IS NULL)))""",
        f"""CREATE TABLE IF NOT EXISTS paper_accounts (
            run_id {txt} PRIMARY KEY REFERENCES paper_canary_runs(run_id),
            starting_cash {m} NOT NULL, cash {m} NOT NULL, equity {m} NOT NULL,
            realized_pnl {m} NOT NULL, gross_exposure {m} NOT NULL, net_exposure {m} NOT NULL,
            version {i} NOT NULL DEFAULT 0, updated_at {ts} NOT NULL,
            CHECK (CAST(starting_cash AS NUMERIC) > 0), CHECK (CAST(cash AS NUMERIC) >= 0),
            CHECK (CAST(gross_exposure AS NUMERIC) >= 0))""",
        f"""CREATE TABLE IF NOT EXISTS paper_orders (
            client_order_id {txt} PRIMARY KEY,
            run_id {txt} NOT NULL REFERENCES paper_canary_runs(run_id),
            idempotency_key {txt} NOT NULL UNIQUE, decision_id {txt} NOT NULL,
            instrument {txt} NOT NULL, side {txt} NOT NULL CHECK (side IN ('BUY','SELL')),
            quantity {m} NOT NULL, order_type {txt} NOT NULL CHECK (order_type = 'MARKET'),
            state {txt} NOT NULL CHECK (state IN ('INTENT','AUTHORIZED','REJECTED','FILLED','CANCELLED')),
            request_checksum {txt} NOT NULL, risk_config_checksum {txt} NOT NULL,
            quote_bid {m} NOT NULL, quote_ask {m} NOT NULL, quote_ts {ts} NOT NULL,
            broker_order_id {txt} UNIQUE, reason {txt}, version {i} NOT NULL DEFAULT 0,
            correlation_id {txt}, created_at {ts} NOT NULL, authorized_at {ts}, terminal_at {ts},
            updated_at {ts} NOT NULL, UNIQUE (run_id, decision_id),
            CHECK (CAST(quantity AS NUMERIC) > 0), CHECK (CAST(quote_bid AS NUMERIC) > 0),
            CHECK (CAST(quote_ask AS NUMERIC) >= CAST(quote_bid AS NUMERIC)))""",
        f"""CREATE TABLE IF NOT EXISTS paper_fills (
            fill_id {txt} PRIMARY KEY,
            client_order_id {txt} NOT NULL UNIQUE REFERENCES paper_orders(client_order_id),
            broker_fill_id {txt} NOT NULL UNIQUE, ledger_seq {i} NOT NULL UNIQUE,
            instrument {txt} NOT NULL, side {txt} NOT NULL CHECK (side IN ('BUY','SELL')),
            quantity {m} NOT NULL, price {m} NOT NULL, commission {m} NOT NULL,
            multiplier {m} NOT NULL, quote_ts {ts} NOT NULL, ts {ts} NOT NULL,
            CHECK (CAST(quantity AS NUMERIC) > 0), CHECK (CAST(price AS NUMERIC) > 0),
            CHECK (CAST(commission AS NUMERIC) >= 0), CHECK (CAST(multiplier AS NUMERIC) = 1))""",
        f"""CREATE TABLE IF NOT EXISTS paper_positions (
            run_id {txt} NOT NULL REFERENCES paper_canary_runs(run_id), instrument {txt} NOT NULL,
            quantity {m} NOT NULL, avg_price {m} NOT NULL, mark_price {m} NOT NULL,
            realized_pnl {m} NOT NULL, version {i} NOT NULL DEFAULT 0, updated_at {ts} NOT NULL,
            PRIMARY KEY (run_id, instrument), CHECK (CAST(quantity AS NUMERIC) >= 0),
            CHECK (CAST(avg_price AS NUMERIC) >= 0), CHECK (CAST(mark_price AS NUMERIC) >= 0))""",
        f"""CREATE TABLE IF NOT EXISTS paper_order_events (
            event_id {txt} PRIMARY KEY,
            client_order_id {txt} NOT NULL REFERENCES paper_orders(client_order_id),
            seq {i} NOT NULL, ts {ts} NOT NULL, event_type {txt} NOT NULL,
            previous_state {txt}, new_state {txt}, reason {txt},
            UNIQUE (client_order_id, seq))""",
        f"""CREATE TABLE IF NOT EXISTS paper_reconciliations (
            reconciliation_id {txt} PRIMARY KEY,
            run_id {txt} NOT NULL REFERENCES paper_canary_runs(run_id),
            status {txt} NOT NULL CHECK (status IN ('PASS','FAIL')),
            fills_checksum {txt} NOT NULL, positions_checksum {txt} NOT NULL,
            account_checksum {txt} NOT NULL, open_order_count {i} NOT NULL,
            breaks_json {txt} NOT NULL, checked_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_paper_runs_status ON paper_canary_runs(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS ix_paper_orders_run_state ON paper_orders(run_id, state, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_paper_orders_run_instrument ON paper_orders(run_id, instrument)",
        "CREATE INDEX IF NOT EXISTS ix_paper_fills_instrument_ts ON paper_fills(instrument, ts)",
        "CREATE INDEX IF NOT EXISTS ix_paper_fills_ledger_seq ON paper_fills(ledger_seq)",
        "CREATE INDEX IF NOT EXISTS ix_paper_positions_run ON paper_positions(run_id, instrument)",
        "CREATE INDEX IF NOT EXISTS ix_paper_order_events_order_ts ON paper_order_events(client_order_id, ts)",
        "CREATE INDEX IF NOT EXISTS ix_paper_reconciliations_run ON paper_reconciliations(run_id, checked_at)",
    ]
    triggers = (
        _paper_canary_triggers_postgres()
        if dialect == "postgres"
        else _paper_canary_triggers_sqlite()
    )
    return tables + indexes + triggers


def _migration_023(dialect: str) -> list[str]:
    """§ Phase P2.1 — durable pre-arm bindings and exact market-event freshness.

    The singleton runtime row carries the exact commit/config/risk tuple that passed the atomic
    pre-arm checks and the one run_id allowed to consume it. Normal ARM/START transitions preserve
    it; DISABLED/KILLED/recovery transitions clear it. Trading Core requires and atomically consumes
    the tuple in the run-creation transaction, preventing a staggered deploy, Risk Control update, or
    second sequential run from silently replacing what the operator prepared.
    """
    t = _types(dialect)
    txt, ts = t["TXT"], t["TS"]
    return [
        f"ALTER TABLE runtime_state ADD COLUMN paper_commit_sha {txt}",
        f"ALTER TABLE runtime_state ADD COLUMN paper_config_checksum {txt}",
        f"ALTER TABLE runtime_state ADD COLUMN paper_risk_config_checksum {txt}",
        f"ALTER TABLE runtime_state ADD COLUMN paper_prepared_at {ts}",
        f"ALTER TABLE runtime_state ADD COLUMN paper_run_id {txt}",
        f"ALTER TABLE market_data_health ADD COLUMN quote_ts {ts}",
    ]


def _migration_024(dialect: str) -> list[str]:
    """§ Phase P2.2 — one durable Paper daily-loss aggregate per UTC trading day.

    A new canary run starts with a fresh account, so account-relative loss cannot be the daily
    authority across sequential runs.  This row accumulates every committed Paper account-equity
    delta for the UTC day and pins the canonical Risk capital that defined that day's loss budget.
    """
    t = _types(dialect)
    ts, txt, i, m = t["TS"], t["TXT"], t["INT"], t["MONEY"]
    return [
        f"""CREATE TABLE IF NOT EXISTS paper_daily_loss_state (
            trade_date {txt} PRIMARY KEY,
            risk_capital_baseline {m} NOT NULL,
            cumulative_equity_delta {m} NOT NULL,
            version {i} NOT NULL DEFAULT 0,
            updated_at {ts} NOT NULL,
            CHECK (CAST(risk_capital_baseline AS NUMERIC) > 0))""",
    ]


def _migration_025(dialect: str) -> list[str]:
    """§ Phase P2.3 — Paper accounts may honestly record insolvency while flattening.

    Migration 22's ``cash >= 0`` check accidentally made a long-reducing SELL impossible when a
    gap down plus commission exhausted the remaining Paper cash.  Starting capital must still be
    positive and gross exposure must still be non-negative, but cash (and the already-unconstrained
    equity/PnL fields) are signed ledger values.

    PostgreSQL can drop the generated v22 constraint in place.  SQLite cannot drop one CHECK from
    an existing table, so rebuild only this projection table inside the migration transaction and
    copy every row losslessly.  No Paper child table references ``paper_accounts``.
    """
    if dialect == "postgres":
        return [
            "ALTER TABLE paper_accounts "
            "DROP CONSTRAINT IF EXISTS paper_accounts_cash_check",
        ]
    t = _types(dialect)
    ts, txt, i, m = t["TS"], t["TXT"], t["INT"], t["MONEY"]
    return [
        f"""CREATE TABLE paper_accounts_v25 (
            run_id {txt} PRIMARY KEY REFERENCES paper_canary_runs(run_id),
            starting_cash {m} NOT NULL, cash {m} NOT NULL, equity {m} NOT NULL,
            realized_pnl {m} NOT NULL, gross_exposure {m} NOT NULL, net_exposure {m} NOT NULL,
            version {i} NOT NULL DEFAULT 0, updated_at {ts} NOT NULL,
            CHECK (CAST(starting_cash AS NUMERIC) > 0),
            CHECK (CAST(gross_exposure AS NUMERIC) >= 0))""",
        "INSERT INTO paper_accounts_v25 "
        "(run_id,starting_cash,cash,equity,realized_pnl,gross_exposure,net_exposure,version,updated_at) "
        "SELECT run_id,starting_cash,cash,equity,realized_pnl,gross_exposure,net_exposure,version,updated_at "
        "FROM paper_accounts",
        "DROP TABLE paper_accounts",
        "ALTER TABLE paper_accounts_v25 RENAME TO paper_accounts",
    ]


# --------------------------------------------------------------------------- § WP2 instrument model triggers
# `instrument_import_events` is an append-only audit/progress log: ANY update or delete is rejected.
# `instrument_import_runs` is status-terminal (RUNNING → COMPLETED|PARTIAL|FAILED): a terminal run is frozen
# and no run may ever be deleted (progress heartbeats while RUNNING remain allowed). The `instruments` table
# itself is a living catalogue (records are re-verified over time) and is therefore intentionally NOT frozen.
_IM_RUN_TERMINAL = "('COMPLETED','PARTIAL','FAILED')"


def _im_triggers_sqlite() -> list[str]:
    return [
        """CREATE TRIGGER IF NOT EXISTS trg_instrument_import_events_no_update
            BEFORE UPDATE ON instrument_import_events
            BEGIN SELECT RAISE(ABORT, 'instrument_import_events: rows are immutable (insert-only)'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_instrument_import_events_no_delete
            BEFORE DELETE ON instrument_import_events
            BEGIN SELECT RAISE(ABORT, 'instrument_import_events: rows cannot be deleted'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS trg_instrument_import_runs_no_update_terminal
            BEFORE UPDATE ON instrument_import_runs WHEN OLD.status IN {_IM_RUN_TERMINAL}
            BEGIN SELECT RAISE(ABORT, 'instrument_import_runs: terminal run is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_instrument_import_runs_no_delete
            BEFORE DELETE ON instrument_import_runs
            BEGIN SELECT RAISE(ABORT, 'instrument_import_runs: runs cannot be deleted'); END""",
    ]


def _im_triggers_postgres() -> list[str]:
    # `%%` is the psycopg literal-percent escape → PL/pgSQL receives a single `%` (the RAISE placeholder).
    return [
        """CREATE OR REPLACE FUNCTION atp_im_block_event_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable (insert-only)', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_im_block_run_update() RETURNS trigger AS $$
           BEGIN IF OLD.status IN ('COMPLETED','PARTIAL','FAILED') THEN
             RAISE EXCEPTION 'instrument_import_runs: terminal run is immutable'; END IF;
             RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_im_block_run_delete() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'instrument_import_runs: runs cannot be deleted'; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS trg_instrument_import_events_no_upd ON instrument_import_events",
        "CREATE TRIGGER trg_instrument_import_events_no_upd BEFORE UPDATE ON instrument_import_events "
        "FOR EACH ROW EXECUTE FUNCTION atp_im_block_event_mutate()",
        "DROP TRIGGER IF EXISTS trg_instrument_import_events_no_del ON instrument_import_events",
        "CREATE TRIGGER trg_instrument_import_events_no_del BEFORE DELETE ON instrument_import_events "
        "FOR EACH ROW EXECUTE FUNCTION atp_im_block_event_mutate()",
        "DROP TRIGGER IF EXISTS trg_instrument_import_runs_no_update ON instrument_import_runs",
        "CREATE TRIGGER trg_instrument_import_runs_no_update BEFORE UPDATE ON instrument_import_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_im_block_run_update()",
        "DROP TRIGGER IF EXISTS trg_instrument_import_runs_no_delete ON instrument_import_runs",
        "CREATE TRIGGER trg_instrument_import_runs_no_delete BEFORE DELETE ON instrument_import_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_im_block_run_delete()",
    ]


def _migration_026(dialect: str) -> list[str]:
    """§ WP2 — persistent, unified global-instrument & market model (REFERENCE DATA ONLY).

    Three additive tables, purely additive to the schema (no existing table is touched):
      * ``instruments`` — one broker-neutral reference record per venue-anchored contract. ``instrument_id``
        is a stable surrogate derived from the natural key; ``natural_key`` is UNIQUE, giving DB-level
        symbol-collision protection across exchanges (same ticker on two venues ⇒ two rows). ``con_id`` is
        UNIQUE where present (multiple NULLs allowed at listing stage). Unknown identifiers (isin/figi/
        cusip/sedol/con_id) are NULL — never fabricated. ``content_checksum`` drives idempotent upserts.
      * ``instrument_import_runs`` — one row per import; resumable + observable (planned/completed/failed
        markets, running counters). PLANNED → RUNNING → COMPLETED|PARTIAL|FAILED; terminal rows are frozen
        by DATABASE triggers, so an import's outcome cannot be silently rewritten.
      * ``instrument_import_events`` — append-only per-market progress/error log (insert-only, DB-enforced),
        giving per-market error isolation an auditable trail.

    NO trading, NO orders/execution/broker, NO market-data subscription, NO IBKR qualification."""
    t = _types(dialect)
    ts, txt, i = t["TS"], t["TXT"], t["INT"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS instruments (
            instrument_id {txt} PRIMARY KEY,
            natural_key {txt} NOT NULL UNIQUE,
            con_id {i} UNIQUE,
            isin {txt}, figi {txt}, cusip {txt}, sedol {txt}, local_symbol {txt},
            symbol {txt} NOT NULL, description {txt},
            region {txt}, country {txt}, exchange {txt} NOT NULL, primary_exchange {txt},
            trading_currency {txt} NOT NULL, settlement_currency {txt},
            timezone {txt}, trading_calendar {txt}, calendar_version {txt},
            asset_class {txt} NOT NULL, sub_class {txt},
            underlying_symbol {txt}, underlying_instrument_id {txt},
            tick_size {txt}, multiplier {txt}, lot_size {txt}, min_size {txt},
            expiry {txt}, strike {txt}, option_right {txt},
            tradability_status {txt} NOT NULL, market_data_status {txt} NOT NULL,
            source_status {txt} NOT NULL, verification_status {txt} NOT NULL,
            source {txt}, last_verified_at {ts},
            content_checksum {txt} NOT NULL,
            created_at {ts} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS instrument_import_runs (
            run_id {txt} PRIMARY KEY, request_checksum {txt} NOT NULL, source_label {txt} NOT NULL,
            planned_markets_json {txt} NOT NULL, completed_markets_json {txt} NOT NULL,
            failed_markets_json {txt} NOT NULL,
            status {txt} NOT NULL CHECK (status IN ('PLANNED','RUNNING','COMPLETED','PARTIAL','FAILED')),
            discovered_count {i} NOT NULL DEFAULT 0, inserted_count {i} NOT NULL DEFAULT 0,
            updated_count {i} NOT NULL DEFAULT 0, unchanged_count {i} NOT NULL DEFAULT 0,
            skipped_count {i} NOT NULL DEFAULT 0, failed_market_count {i} NOT NULL DEFAULT 0,
            failure_code {txt}, failure_reason {txt},
            created_at {ts} NOT NULL, started_at {ts}, ended_at {ts}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS instrument_import_events (
            id {txt} PRIMARY KEY,
            run_id {txt} NOT NULL REFERENCES instrument_import_runs(run_id),
            seq {i}, ts {ts}, market {txt}, event_type {txt} NOT NULL, severity {txt},
            details_json {txt}, created_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_instruments_symbol_exch ON instruments(symbol, exchange)",
        "CREATE INDEX IF NOT EXISTS ix_instruments_asset_class ON instruments(asset_class)",
        "CREATE INDEX IF NOT EXISTS ix_instruments_region ON instruments(region, country)",
        "CREATE INDEX IF NOT EXISTS ix_instruments_underlying ON instruments(underlying_symbol)",
        "CREATE INDEX IF NOT EXISTS ix_instruments_verification ON instruments(verification_status)",
        "CREATE INDEX IF NOT EXISTS ix_instrument_import_runs_req "
        "ON instrument_import_runs(request_checksum, status)",
        "CREATE INDEX IF NOT EXISTS ix_instrument_import_events_run "
        "ON instrument_import_events(run_id, event_type)",
    ]
    triggers = _im_triggers_postgres() if dialect == "postgres" else _im_triggers_sqlite()
    return tables + indexes + triggers


# --------------------------------------------------------------------------- § WP5 news / filings triggers
# Original news messages, their instrument mappings, message events (corrections/retractions/audit) and
# import events are INSERT-ONLY: any update or delete is rejected — an original is never overwritten, a
# correction/retraction is a NEW record. `news_import_runs` is status-terminal (frozen once terminal). The
# `news_sources` registry is intentionally mutable (availability / last-success / last-error change).
_NEWS_IMMUTABLE_TABLES = ("news_messages", "news_message_instruments", "news_message_events",
                          "news_import_events")
_NEWS_RUN_TERMINAL = "('COMPLETED','PARTIAL','FAILED')"


def _news_triggers_sqlite() -> list[str]:
    out: list[str] = []
    for tbl in _NEWS_IMMUTABLE_TABLES:
        out += [
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_update BEFORE UPDATE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows are immutable (insert-only)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_delete BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows cannot be deleted'); END""",
        ]
    out += [
        f"""CREATE TRIGGER IF NOT EXISTS trg_news_import_runs_no_update_terminal
            BEFORE UPDATE ON news_import_runs WHEN OLD.status IN {_NEWS_RUN_TERMINAL}
            BEGIN SELECT RAISE(ABORT, 'news_import_runs: terminal run is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_news_import_runs_no_delete
            BEFORE DELETE ON news_import_runs
            BEGIN SELECT RAISE(ABORT, 'news_import_runs: runs cannot be deleted'); END""",
    ]
    return out


def _news_triggers_postgres() -> list[str]:
    # `%%` is the psycopg literal-percent escape → PL/pgSQL receives a single `%` (the RAISE placeholder).
    out = [
        """CREATE OR REPLACE FUNCTION atp_news_block_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable (insert-only)', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_news_block_run_update() RETURNS trigger AS $$
           BEGIN IF OLD.status IN ('COMPLETED','PARTIAL','FAILED') THEN
             RAISE EXCEPTION 'news_import_runs: terminal run is immutable'; END IF;
             RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE FUNCTION atp_news_block_run_delete() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'news_import_runs: runs cannot be deleted'; END; $$ LANGUAGE plpgsql""",
    ]
    for tbl in _NEWS_IMMUTABLE_TABLES:
        out += [
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_upd ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_upd BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_news_block_mutate()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_del ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_del BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_news_block_mutate()",
        ]
    out += [
        "DROP TRIGGER IF EXISTS trg_news_import_runs_no_update ON news_import_runs",
        "CREATE TRIGGER trg_news_import_runs_no_update BEFORE UPDATE ON news_import_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_news_block_run_update()",
        "DROP TRIGGER IF EXISTS trg_news_import_runs_no_delete ON news_import_runs",
        "CREATE TRIGGER trg_news_import_runs_no_delete BEFORE DELETE ON news_import_runs "
        "FOR EACH ROW EXECUTE FUNCTION atp_news_block_run_delete()",
    ]
    return out


def _migration_027(dialect: str) -> list[str]:
    """§ WP5 — worldwide company news, official filings & regulatory publications (RESEARCH DATA ONLY).

    Purely additive: six new tables, no existing table touched (`news_items`/`companies` stay as they are).
    The ORIGINAL message (`news_messages`) is immutable and never overwritten; corrections/retractions are
    NEW rows linked via correction_of_id/retraction_of_id, and `news_message_events` is an append-only,
    DB-immutable correction/retraction/audit log. Instrument mapping (`news_message_instruments`, FK to the
    WP2 `instruments`) is fail-closed (VERIFIED/AMBIGUOUS/UNMAPPED) — a symbol alone never maps uniquely, and
    symbols of different exchanges stay separate. `news_sources` records each source's explicit license +
    usage rights + availability. `news_import_runs`/`news_import_events` are a resumable, observable,
    per-provider/region-isolated import lifecycle. NO trading, NO orders/execution, NO news/subscription
    purchase, NO HTTP write path. Classification/relevance/sentiment/impact are research metadata, not facts."""
    t = _types(dialect)
    ts, txt, i, b = t["TS"], t["TXT"], t["INT"], t["BOOL"]
    tables = [
        f"""CREATE TABLE IF NOT EXISTS news_messages (
            message_id {txt} PRIMARY KEY, provider {txt} NOT NULL, provider_id {txt} NOT NULL,
            source_id {txt} NOT NULL, source_type {txt} NOT NULL, primacy {txt} NOT NULL,
            original_title {txt} NOT NULL, original_body {txt}, original_language {txt},
            translated_title {txt}, translated_summary {txt}, translation_status {txt} NOT NULL,
            translation_source {txt}, url {txt},
            published_at {ts}, received_at {ts}, correction_at {ts},
            event_category {txt} NOT NULL, relevance {txt} NOT NULL, impact_estimate {txt} NOT NULL,
            uncertainty {txt} NOT NULL, source_confidence {txt} NOT NULL,
            license_status {txt} NOT NULL, storage_status {txt} NOT NULL, time_status {txt} NOT NULL,
            content_checksum {txt} NOT NULL, cluster_id {txt},
            correction_of_id {txt}, retraction_of_id {txt}, supersedes_id {txt}, duplicate_of_id {txt},
            affected_countries_json {txt} NOT NULL DEFAULT '[]', affected_regions_json {txt} NOT NULL DEFAULT '[]',
            affected_industries_json {txt} NOT NULL DEFAULT '[]', affected_companies_json {txt} NOT NULL DEFAULT '[]',
            affected_exchanges_json {txt} NOT NULL DEFAULT '[]', provenance_json {txt} NOT NULL DEFAULT '{{}}',
            created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS news_message_instruments (
            message_id {txt} NOT NULL REFERENCES news_messages(message_id),
            instrument_id {txt} NOT NULL REFERENCES instruments(instrument_id),
            mapping_status {txt} NOT NULL, confidence {txt}, method {txt}, created_at {ts} NOT NULL,
            PRIMARY KEY (message_id, instrument_id))""",
        f"""CREATE TABLE IF NOT EXISTS news_message_events (
            id {txt} PRIMARY KEY, message_id {txt} NOT NULL REFERENCES news_messages(message_id),
            seq {i}, ts {ts}, event_type {txt} NOT NULL, severity {txt}, related_message_id {txt},
            details_json {txt}, created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS news_sources (
            source_id {txt} PRIMARY KEY, name {txt} NOT NULL, source_type {txt} NOT NULL, primacy {txt} NOT NULL,
            regions_json {txt} NOT NULL DEFAULT '[]', languages_json {txt} NOT NULL DEFAULT '[]',
            update_mode {txt} NOT NULL, rate_limit_json {txt} NOT NULL DEFAULT '{{}}',
            license_status {txt} NOT NULL, storage_allowed {b} NOT NULL DEFAULT 0,
            redistribution_allowed {b} NOT NULL DEFAULT 0, commercial_use_allowed {b} NOT NULL DEFAULT 0,
            attribution_required {b} NOT NULL DEFAULT 1, available {b} NOT NULL DEFAULT 0,
            last_success_at {ts}, last_error {txt}, created_at {ts} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS news_import_runs (
            run_id {txt} PRIMARY KEY, request_checksum {txt} NOT NULL, run_label {txt} NOT NULL,
            provider {txt} NOT NULL, source_id {txt} NOT NULL, cursor {txt},
            status {txt} NOT NULL CHECK (status IN ('PLANNED','RUNNING','COMPLETED','PARTIAL','FAILED')),
            completed_regions_json {txt} NOT NULL, failed_regions_json {txt} NOT NULL,
            fetched_count {i} NOT NULL DEFAULT 0, stored_count {i} NOT NULL DEFAULT 0,
            duplicate_count {i} NOT NULL DEFAULT 0, ambiguous_count {i} NOT NULL DEFAULT 0,
            correction_count {i} NOT NULL DEFAULT 0, retraction_count {i} NOT NULL DEFAULT 0,
            unmapped_count {i} NOT NULL DEFAULT 0, error_count {i} NOT NULL DEFAULT 0,
            failure_code {txt}, failure_reason {txt},
            created_at {ts} NOT NULL, started_at {ts}, ended_at {ts}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS news_import_events (
            id {txt} PRIMARY KEY, run_id {txt} NOT NULL REFERENCES news_import_runs(run_id),
            seq {i}, ts {ts}, provider {txt}, region {txt}, message_id {txt}, event_type {txt} NOT NULL,
            severity {txt}, reason {txt}, details_json {txt}, created_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_news_messages_checksum ON news_messages(content_checksum)",
        "CREATE INDEX IF NOT EXISTS ix_news_messages_cluster ON news_messages(cluster_id)",
        "CREATE INDEX IF NOT EXISTS ix_news_messages_published ON news_messages(published_at)",
        "CREATE INDEX IF NOT EXISTS ix_news_messages_source ON news_messages(source_id, event_category)",
        "CREATE INDEX IF NOT EXISTS ix_news_msg_instruments_inst ON news_message_instruments(instrument_id, mapping_status)",
        "CREATE INDEX IF NOT EXISTS ix_news_message_events_msg ON news_message_events(message_id, event_type)",
        "CREATE INDEX IF NOT EXISTS ix_news_import_runs_req ON news_import_runs(request_checksum, status)",
        "CREATE INDEX IF NOT EXISTS ix_news_import_events_run ON news_import_events(run_id, event_type)",
    ]
    triggers = _news_triggers_postgres() if dialect == "postgres" else _news_triggers_sqlite()
    return tables + indexes + triggers


# --------------------------------------------------------------------------- § WP6 macro / geopolitical events
# `macro_events` is an INSERT-ONLY overlay on the immutable WP5 `news_messages`: one macro row per newsroom
# message, never updated or deleted (corrections/retractions are NEW newsroom records, mirrored here). The
# `macro_sources` registry is intentionally mutable (availability / last-success / last-error change).
_MACRO_IMMUTABLE_TABLES = ("macro_events",)


def _macro_triggers_sqlite() -> list[str]:
    out: list[str] = []
    for tbl in _MACRO_IMMUTABLE_TABLES:
        out += [
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_update BEFORE UPDATE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows are immutable (insert-only)'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS trg_{tbl}_no_delete BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, '{tbl}: rows cannot be deleted'); END""",
        ]
    return out


def _macro_triggers_postgres() -> list[str]:
    out = [
        """CREATE OR REPLACE FUNCTION atp_macro_block_mutate() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION '%%: rows are immutable (insert-only)', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""",
    ]
    for tbl in _MACRO_IMMUTABLE_TABLES:
        out += [
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_upd ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_upd BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_macro_block_mutate()",
            f"DROP TRIGGER IF EXISTS trg_{tbl}_no_del ON {tbl}",
            f"CREATE TRIGGER trg_{tbl}_no_del BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION atp_macro_block_mutate()",
        ]
    return out


def _migration_029(dialect: str) -> list[str]:
    """§ WP6 — worldwide macro / geopolitical / regulatory event intake (RESEARCH DATA ONLY).

    Purely additive: two new tables, no existing table touched. `macro_events` is a per-message OVERLAY on the
    immutable WP5 `news_messages` (FK message_id) that adds macro-specific structure — event sub-type, source
    class, geographic scope, severity (research metadata, not a fact), affected regions/countries/blocs/asset
    CLASSES, a fail-closed instrument link-status, and a macro-situation cluster id. It is INSERT-ONLY and
    DB-immutable; corrections/retractions are NEW newsroom records (mirrored here). Instrument linkage reuses
    the fail-closed WP5 `news_message_instruments`. `macro_sources` records each macro channel's explicit
    mandate + license + usage rights + availability (fail-closed until an entitled source attaches). The
    import lifecycle REUSES the WP5 `news_import_runs`/`news_import_events` (a macro event is a newsroom
    record). NO trading, NO orders/execution, NO subscription purchase, NO HTTP write path. Numbered 29 (not
    28) so it never collides with WP4's migration 28 on the sibling stack; the framework applies migrations by
    set-difference, so the 28 gap on this branch is intentional and harmless.
    """
    t = _types(dialect)
    ts, txt, b = t["TS"], t["TXT"], t["BOOL"]      # macro tables use no INT columns
    tables = [
        f"""CREATE TABLE IF NOT EXISTS macro_events (
            message_id {txt} PRIMARY KEY REFERENCES news_messages(message_id),
            macro_type {txt} NOT NULL, source_class {txt} NOT NULL, geo_scope {txt} NOT NULL,
            severity {txt} NOT NULL, policy_area {txt},
            affected_regions_json {txt} NOT NULL DEFAULT '[]', affected_countries_json {txt} NOT NULL DEFAULT '[]',
            affected_blocs_json {txt} NOT NULL DEFAULT '[]', affected_asset_classes_json {txt} NOT NULL DEFAULT '[]',
            link_status {txt} NOT NULL, macro_cluster_id {txt}, macro_checksum {txt} NOT NULL,
            correction_of_id {txt}, retraction_of_id {txt}, provenance_json {txt} NOT NULL DEFAULT '{{}}',
            created_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS macro_sources (
            source_id {txt} PRIMARY KEY, name {txt} NOT NULL, source_class {txt} NOT NULL,
            regions_json {txt} NOT NULL DEFAULT '[]', languages_json {txt} NOT NULL DEFAULT '[]',
            mandate {txt} NOT NULL, update_mode {txt} NOT NULL, rate_limit_json {txt} NOT NULL DEFAULT '{{}}',
            license_status {txt} NOT NULL, storage_allowed {b} NOT NULL DEFAULT 0,
            redistribution_allowed {b} NOT NULL DEFAULT 0, commercial_use_allowed {b} NOT NULL DEFAULT 0,
            attribution_required {b} NOT NULL DEFAULT 1, available {b} NOT NULL DEFAULT 0,
            last_success_at {ts}, last_error {txt}, created_at {ts} NOT NULL, updated_at {ts} NOT NULL)""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_macro_events_cluster ON macro_events(macro_cluster_id)",
        "CREATE INDEX IF NOT EXISTS ix_macro_events_type ON macro_events(macro_type, geo_scope)",
        "CREATE INDEX IF NOT EXISTS ix_macro_events_checksum ON macro_events(macro_checksum)",
        "CREATE INDEX IF NOT EXISTS ix_macro_events_link ON macro_events(link_status)",
        "CREATE INDEX IF NOT EXISTS ix_macro_events_source_class ON macro_events(source_class)",
    ]
    triggers = _macro_triggers_postgres() if dialect == "postgres" else _macro_triggers_sqlite()
    return tables + indexes + triggers


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
    (20, "research_datasets", _migration_020),
    (21, "research_intel_validation", _migration_021),
    (22, "durable_paper_canary", _migration_022),
    (23, "paper_canary_operator_bindings", _migration_023),
    (24, "paper_canary_daily_loss_aggregate", _migration_024),
    (25, "paper_canary_signed_account_ledger", _migration_025),
    (26, "global_instrument_model", _migration_026),
    (27, "global_news_official_filings", _migration_027),
    # NOTE: 28 is intentionally skipped on this stack — it belongs to WP4 on the sibling stack. The migrator
    # applies by set-difference (no contiguity requirement), so the gap is harmless and avoids a collision.
    (29, "macro_geopolitical_events", _migration_029),
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
