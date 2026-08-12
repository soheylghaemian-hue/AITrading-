# Build Status — Complete Trading Brain (§17 checklist)

Maps the target checklist to the code that implements it. **Broker-independent components are
complete and tested** (280 tests, 102 modules). What remains needs real data/gateway access,
not new architecture. No fabricated performance anywhere — synthetic inputs are used only in
tests and are never presented as trading results (§14). **Status: PRE-IBKR CODE FREEZE.**

| Target (§17) | Module(s) | Status |
|--------------|-----------|--------|
| Multi-Agent Architecture (9 traders) | `atp.strategy.*` | ✅ 9/9 specialists |
| Market Regime Engine | `atp.regime` | ✅ |
| Opportunity Engine | `atp.opportunity` | ✅ |
| **Master Portfolio Manager** | `atp.portfolio` | ✅ (budget, count, diversification, cash-valid) |
| **Risk Engine** (absolute veto) | `atp.risk` | ✅ hardened — every §4 scenario tested |
| Learning / Experience Engine | `atp.journal` | ✅ full §1 record fields (agent, signal strength, expected risk, stop/target, financing, strategy version) |
| Strategy Discovery Framework | `atp.discovery` | ✅ (OOS/walk-forward/Monte-Carlo gauntlet) |
| Backtesting Engine | `atp.backtest` | ✅ spread/commission/slippage/impact + session/holiday gating + cost models |
| Cost models (§20) | `atp.costs` | ✅ commission/slippage/financing/borrow/FX (data-independent; rates/FX injected) |
| Corporate actions / rollover / expiry (§3) | `atp.corpactions` | ✅ data models + processors (data caller-supplied) |
| Market calendar gating (§3) | `atp.instruments.calendar` + desk | ✅ session hours + holidays → no trade when closed |
| Model Governance | `atp.governance` | ✅ versioning + validated promotion + full lifecycle (RESEARCH→…→LIVE→SUSPENDED→RETIRED) + model metadata |
| Model Decay Detection | `atp.governance.decay` | ✅ |
| **Instrument Master** | `atp.instruments` | ✅ (full spec + underlying relationships + calendar) |
| **Market Universe Scanner** | `atp.scanner` | ✅ (hierarchical liquidity→vol→momentum funnel) |
| **Data Quality Engine** | `atp.dataquality` | ✅ (stale/impossible/duplicate/spread/disconnect → NO TRADE) |
| Broker Reconciliation | `atp.brokers.reconcile` | ✅ positions + cash + realized P&L + open orders |
| Paper Execution | `atp.brokers.paper` + `atp.execution` | ✅ realistic fills, impact, TWAP/VWAP, spreads, settlement |
| **IBKR Adapter prepared** | `atp.brokers.ibkr`, `atp.live.feed` | ✅ connect/data/historical/orders/executions/order-status/reconnect/error-handling (mapping tested; live validation tomorrow) |
| Automated Tests | `tests/` | ✅ 280 |
| Docker | `Dockerfile`, `docker-compose.yml` | ✅ |
| Documentation | `docs/`, `README.md` | ✅ |
| Application / CLI | `atp.app`, `atp.config`, `python -m atp` | ✅ |
| Command Center backend — read-only `/dashboard/*` API + snapshot + emergency stop (§13/§30) | `atp.dashboard` | ✅ real data, empty states, no fakes |
| Notifications (§23) | `atp.dashboard.notifications` | ✅ |
| Command Center frontend | `atp.dashboard.static` | ✅ dark placeholder page; **Next.js app deferred per spec §34** until after the IBKR end-to-end test |

## Explicitly still open (needs real data or an external runtime)

The friction *models and interfaces* are built and tested; what remains is feeding them **real
reference data** (which arrives with IBKR / a data vendor):

1. **Real exchange calendars** (per-venue holidays / half-days) to populate `MarketCalendar`.
2. **Real corporate-action / dividend / split feeds** and **real futures roll schedules** to
   populate `CorporateActionsBook` / `RollCalendar` (the processors are done and tested).
3. **Real FX rates** for `FXConverter` (the conversion-cost model is done; rates are injected,
   missing → `None`, never invented).
4. **Live IBKR validation** + real market-data feeds. *Needs the gateway (tomorrow).*
5. **Next.js frontend** — deprioritized per instructions until the end-to-end data test passes.

Everything broker-independent in the §17 checklist is now built and tested.

## Environment note

Tests run green via `python -m pytest` from an accessible working directory. Because the repo
sits under macOS TCC-protected `~/Documents`, `getcwd()` is intermittently denied there, which
breaks pytest's startup. Fix in a fresh session: grant Full Disk Access to the terminal, or
relocate the repo outside `~/Documents`. See the memory note `trading-env-tcc-getcwd`.
