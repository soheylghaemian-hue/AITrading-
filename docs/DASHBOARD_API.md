# Command Center — Dashboard API contract (§30)

The backend for the AI Trading Desk Command Center. Read-only endpoints serve one snapshot of
real engine state; one protected control trips the Risk Engine kill switch. **The dashboard
never makes or overrides trading decisions** (§31) — the Trading Engine, Portfolio Manager and
Risk Engine are authoritative. **No fabricated data** (§33): where there is no data, fields are
`null`/empty and the UI shows `NO DATA`.

The production **Next.js/React/TypeScript** frontend (spec §1–§35) is built against these
contracts and is deliberately deferred until after the IBKR end-to-end test (§34) — a
functional dark placeholder page ships at `/` today.

## Serving it (live only — needs FastAPI)

```python
from atp.dashboard.api import DashboardContext, create_app
ctx = DashboardContext(broker=broker, risk=risk, desk=desk, journal=journal,
                       registry=registry, notifications=nc, mode="paper")
app = create_app(ctx)   # then: uvicorn ... (pip install -e ".[live]")
```

## Read-only endpoints

| Method & path | Returns |
|---------------|---------|
| `GET /api/health` | `{status:"ok"}` |
| `GET /dashboard/summary` (alias `GET /api/snapshot`) | the full snapshot (below) |
| `GET /dashboard/positions` | open positions |
| `GET /dashboard/risk` | risk view (limits, drawdown, kill/halt/broker flags) |
| `GET /dashboard/agents` | AI-team status + recorded edge per specialist |
| `GET /dashboard/opportunities` | current per-instrument market view (regime/price/trend) |
| `GET /dashboard/performance` | overall analytics (win rate, PF, expectancy, …) |
| `GET /dashboard/governance` | strategy governance states |
| `GET /dashboard/system` | component health map |
| `GET /dashboard/notifications` | recent notifications (severity-ranked) |
| `GET /dashboard/reconciliation` | positions + risk (full recon runs in the engine) |
| `GET /` | the bundled dark command-center page |

## Protected control (§13)

| Method & path | Effect |
|---------------|--------|
| `POST /dashboard/emergency-stop` | trips `RiskEngine.kill_switch` — stops **all** new orders (does not auto-flatten) |
| `POST /dashboard/resume` | clears the kill switch (authorized users only) |

Both require `Authorization: Bearer <ATP_DASHBOARD_TOKEN>`. If the env var is unset, mutations
return `503` (read-only server). **The token is never hard-coded and never shipped to the
frontend.** IBKR credentials, API keys and secrets never appear in the frontend, source, docs
or this repo (§28) — they live only in the local IB Gateway / secure environment.

## Snapshot shape (the one source of truth)

```
{
  generated_at, mode ("paper"|"live"), system_status ("online"|"degraded"|"halted"),
  account: { equity, cash, realized_pnl, unrealized_pnl, gross_exposure, net_exposure, gross_leverage },
  risk:    { halted, halt_reason, killed, kill_reason, broker_connected, drawdown, daily_pnl,
             daily_loss_pct, gross_leverage, max_daily_loss_pct, max_drawdown_pct,
             max_gross_leverage, max_position_pct, max_correlated_exposure_pct },
  positions: [ { key, symbol, asset_class, quantity, avg_price, market_price, notional, unrealized_pnl, side } ],
  market:  { "<key>": { regime, price, trend, realized_vol, vol_percentile, ready } },
  agents:  [ { name, status, trades, win_rate, expectancy, total_pnl, profit_factor, avg_confidence, version } ],
  governance: [ { name, status, version, reason, since } ],
  system_health: { trading_engine, risk_engine, execution_engine, broker, market_data,
                   learning_engine, database, redis, api },   // online|degraded|offline|unknown
  hero: { scanned, opportunities, after_liquidity, after_statistical, portfolio_approved, risk_approved },  // null until a real scan
  analytics_overall, analytics_by_strategy, analytics_by_regime,
  recent_trades: [ ...TradeRecord fields... ],
  notifications: [ { ts, severity, kind, message } ],
  n_trades
}
```

Every value is derived from real engine state; `hero.*` and unknown `system_health.*` are
`null`/`"unknown"` until the relevant data source (scanner, DB, gateway) is connected.
