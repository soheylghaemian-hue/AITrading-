# atp — Autonomous Multi-Asset Trading platform

An implementation of the **Gesamtkonzept V1.0**: a modular, broker-agnostic *digital trading
desk* — not a single bot. Specialized traders propose, a portfolio/opportunity layer ranks,
an **independent Risk Engine vetoes**, and an execution layer routes to a broker adapter. The
same desk object is driven by both the backtester and (later) the live loop, so backtest and
live behavior cannot silently diverge.

> This repository provides the **machinery** to research, validate and — only after paper
> trading and an explicit policy — autonomously execute strategies under risk control.
> It makes **no** profitability claim. Every number it reports is computed from real fed data.

## The core loop (§6)

```
market data → normalize → FEATURES → REGIME → specialist SIGNALS →
OPPORTUNITY ranking → PORTFOLIO target + SIZING → RISK VETO → EXECUTION → BROKER
                                   ↑                                          │
                                   └──────── monitor · learn · improve ◄──────┘
```

## What runs today

| Layer | Module | Status |
|-------|--------|--------|
| Core types (enums, immutable events) | `atp.core` | ✅ |
| Trading Policy (the one human decision, §15) | `atp.policy` | ✅ |
| Broker abstraction + deterministic PaperBroker (§3/§17) | `atp.brokers` | ✅ paper |
| IBKR / IB Gateway adapter — equities/FX/futures/options (§17) | `atp.brokers.ibkr` | ✅ mapping tested, live paper next |
| Position reconciliation + halt-on-mismatch (§17/§18) | `atp.brokers.reconcile` | ✅ |
| Feature Engine (rolling, no look-ahead, §5) | `atp.features` | ✅ |
| Market Regime classifier (§7) | `atp.regime` | ✅ rule-based |
| Specialists — Momentum, MeanRev, Cross-Asset, Breakout, StatArb, Vol, FX-Carry, Macro, Event (§8) | `atp.strategy` | ✅ 9 of 9 |
| Cross-Asset Intelligence — correlation & divergence (§6) | `atp.cross_asset` | ✅ |
| Statistical Arbitrage — pairs spread engine (§8) | `atp.stat_arb` | ✅ |
| Options — Black–Scholes, greeks, IV, chains, spreads + settlement (§5/§16) | `atp.options` | ✅ |
| Macro — policy rates, carry, rate trend, events calendar (§5) | `atp.macro` | ✅ |
| Opportunity Engine — cross-asset scoring (§10) | `atp.opportunity` | ✅ |
| Position Sizer — risk-based (§10/§15) | `atp.opportunity.sizing` | ✅ |
| Risk Engine — absolute veto + protective halt (§14) | `atp.risk` | ✅ |
| Execution Engine — risk-gated routing, slicing, impact, TWAP/VWAP (§16) | `atp.execution` | ✅ |
| Autonomous Trading Desk — orchestrator (§3/§6) | `atp.desk` | ✅ |
| Event-driven Backtester + metrics (§11/§13) | `atp.backtest` | ✅ |
| Validation — OOS / walk-forward / Monte-Carlo (§11) | `atp.backtest.validation` | ✅ |
| Experience journal — fills→trades, MFE/MAE, SQLite (§11) | `atp.journal` | ✅ |
| Trade analytics — edge by strategy & regime (§11) | `atp.journal.analytics` | ✅ |
| Governance — decay suspension + model versioning (§19) | `atp.governance` | ✅ |
| Strategy Discovery — candidate search + validation gauntlet (§12) | `atp.discovery` | ✅ |
| Instrument Master — full reference model + underlying families (§5) | `atp.instruments` | ✅ |
| Master Portfolio Manager — allocation across opportunities (§9) | `atp.portfolio` | ✅ |
| Data Quality Engine — NO-TRADE gate for bad data (§10) | `atp.dataquality` | ✅ |
| Market Universe Scanner — hierarchical discovery funnel (§6) | `atp.scanner` | ✅ |
| Risk Engine — hardened; every §4 scenario tested (§14) | `atp.risk` | ✅ |
| Application — config-driven assembly + `python -m atp` CLI (§15/§24) | `atp.app`, `atp.config` | ✅ |
| Live/paper runner — feed→desk, reconcile + govern in-loop (§17) | `atp.live` | ✅ paper |
| Pluggable context feeds — options/rates/events into engines (§5) | `atp.feeds` | ✅ |
| Dashboard — read-model snapshot + FastAPI + static page (§22) | `atp.dashboard` | ✅ |
| Persistence — Redis state store + Postgres journal (§21) | `atp.persistence` | ✅ |

**What's left is integration/ops, not new architecture.** Every core engine, all nine §8
specialists, the full §16 execution stack (impact, in-step slicing, TWAP/VWAP, option spreads,
cash **and** physical settlement), governance, discovery, the live loop, persistence and the
pluggable feed seam exist and are tested. What remains: connecting **real data providers**
behind the `atp.feeds` interfaces (tested synthetic ones ship; a production adapter is a
drop-in for market data, options chains, macro rates, events) and a **running IB Gateway**
(offline tests cover the mapping, not a live socket — use `examples/ibkr_smoke.py`), plus the
production **Next.js/React frontend** (a read-only API + static dashboard page ship today, §22).
See `docs/DECISIONS.md`.

## Run it

```bash
PYTHONPATH=src python3 -m atp backtest --bars 400   # the whole pipeline as one command (§24)
PYTHONPATH=src python3 -m atp config                # print the Trading Policy / system config
python3 -m pytest                                  # 280 tests, stdlib-only, ~0.7s
PYTHONPATH=src python3 examples/run_backtest.py     # full pipeline demo on synthetic data
PYTHONPATH=src python3 examples/analyze_journal.py  # experience journal + §11 edge breakdown
PYTHONPATH=src python3 examples/governance_demo.py  # §19 decay suspension + model versioning
PYTHONPATH=src python3 examples/discovery_demo.py   # §12 discovery + validation gauntlet
PYTHONPATH=src python3 examples/live_demo.py        # §17 live/paper loop + in-loop govern
PYTHONPATH=src python3 examples/cross_asset_demo.py  # §6 cross-asset correlation & divergence
PYTHONPATH=src python3 examples/execution_demo.py   # §16 slicing vs one-shot under impact
PYTHONPATH=src python3 examples/statarb_demo.py     # §8 statistical-arbitrage pairs trade
PYTHONPATH=src python3 examples/options_demo.py     # §5 options pricing/greeks/IV + vol specialist
PYTHONPATH=src python3 examples/macro_demo.py       # §5 rates/carry + FX-carry & macro specialists
PYTHONPATH=src python3 examples/event_demo.py       # §8 event specialist: de-risk + trade surprise
PYTHONPATH=src python3 examples/twap_demo.py        # §16 TWAP/VWAP time-scheduled execution
PYTHONPATH=src python3 examples/options_spread_demo.py  # §16 option spreads + expiry settlement
PYTHONPATH=src python3 examples/dashboard_snapshot.py  # write a real snapshot for the dashboard
# then view the dashboard (static, real data):
python3 -m http.server 8000 --directory src/atp/dashboard/static   # open http://localhost:8000/
# validate a real IB Gateway (your machine, paper account):
PYTHONPATH=src python3 examples/ibkr_smoke.py --port 4002
```

The core and the whole test suite are **dependency-free** (Python stdlib). Live/ML extras are
optional: `pip install -e ".[live,ml,dev]"`.

## Design commitments

- **Risk has the final word.** Every order is checked *after* sizing and *before* the broker;
  any breach → NO TRADE. A halt actively *flattens* open positions rather than riding them.
- **No look-ahead, by construction.** Features and strategies only ever see bars up to `now`;
  the backtester replays chronologically through the same desk used live.
- **Honest accounting.** `equity = cash + market value of positions`, net of commission,
  spread and slippage. `realized_pnl` changes only when a position is reduced/closed.
- **No fabricated performance.** Metrics are pure functions of the equity curve and closed
  trades; annualized figures are suppressed on samples too short to annualize.

## Security (§23)

No credentials in source. Secrets go in the environment / a secrets manager (`.env` is
git-ignored). Paper and live credentials are kept separate; live trading is gated behind
explicit configuration.

## Web Command Center (`frontend/`)

A production **Next.js 14 / React / TypeScript** dashboard renders the read-only Command Center.
It is a **pure consumer of the backend Dashboard API contract** (`atp.dashboard.api`) — it holds
no business logic and no credentials.

- **Pages:** `/` and `/dashboard` (overview) plus `/dashboard/{positions,opportunities,agents,`
  `risk,performance,governance,system,reconciliation}`.
- **Data:** polls `GET ${NEXT_PUBLIC_API_BASE_URL}/dashboard/summary` every 4 s. When no backend
  is configured/reachable (e.g. a public deploy), every field shows **NO DATA** — never a
  fabricated value, never a fake 0, never cached data presented as live.
- **Market-data states** are shown distinctly: `REALTIME` · `DELAYED` · `STALE` ·
  `NOT_AVAILABLE` · `ERROR` (with the IBKR reason/error code).
- **Local dev:** `cd frontend && npm install && npm run dev` → http://localhost:3000. Set
  `NEXT_PUBLIC_API_BASE_URL` in `frontend/.env.local` to a reachable read-only backend.
- **Tests:** `cd frontend && npm test` (Vitest — formatters, NO-DATA rendering, market-data
  states, and a source scan proving no secrets / no broker access in the frontend).

### Safety model (public frontend)

The public dashboard is **structurally incapable** of trading:

- It **never** connects to IB Gateway (`localhost:4002/4001`), never imports `ib_insync`, never
  calls `placeOrder`/`cancelOrder`/`reqMktData`. A unit test enforces this by scanning the source.
- It reads **only** `/dashboard/*` endpoints. The **Emergency Stop** sends a token-authenticated
  `POST /dashboard/emergency-stop` to the backend; the authoritative kill switch lives in the
  backend `RiskEngine` — the browser cannot touch the broker.
- Only `NEXT_PUBLIC_*` env vars are readable in the browser (enforced by test). `ATP_DASHBOARD_TOKEN`
  and all IBKR credentials live **only** in the backend / local IB Gateway environment.

## Deployment (GitHub → Vercel)

The Python trading engine + IB Gateway stay **local/private**; only the frontend is deployed.

1. **Backend (local, private):** run the read-only Dashboard API with `pip install -e ".[live]"`
   and serve `atp.dashboard.api:create_app(context)` via uvicorn. Never expose IB Gateway to the
   internet; expose only the read-only Dashboard API (behind a tunnel/auth) if you want live data.
2. **Frontend (Vercel):** import the GitHub repo into Vercel, framework **Next.js**, root
   directory `frontend/`, build `next build`. Set `NEXT_PUBLIC_API_BASE_URL` (empty ⇒ NO DATA;
   or a reachable read-only backend URL). Never set broker secrets in Vercel.
3. The free `*.vercel.app` domain is used first; a custom domain is optional later.

**IBKR stays PAPER · READ-ONLY · NO EXECUTION throughout.** This phase changed no Risk Engine,
Execution Engine, IBKR adapter or strategy code — only the web UI and deployment scaffolding.
