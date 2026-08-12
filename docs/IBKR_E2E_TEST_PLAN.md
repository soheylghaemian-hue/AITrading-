# IBKR Paper End-to-End Test Plan (§17/§24)

Run **in order**, phase by phase, only after `examples/ibkr_smoke.py` passes read-only.
**Paper account only. No live orders. No fabricated market data. No invented performance.**
Stop at the first failed phase and apply its Safety Action before continuing.

Legend — each phase: **Input** · **Expected Output** · **Pass** · **Fail** · **Safety Action**.

---

## PHASE A — CONNECTIVITY
- **Input:** IB Gateway (paper, port 4002) running; `IBKRBroker.connect()`.
- **Expected:** socket connects; `is_connected()` → true.
- **Pass:** connected within timeout, no error events 502/504/1100.
- **Fail:** connect throws, or connectivity error code received.
- **Safety:** do not proceed; fix gateway/API settings; `RiskEngine.set_broker_connected(False)`.

## PHASE B — ACCOUNT
- **Input:** `get_account()`, `get_positions()`, `open_orders()`.
- **Expected:** NetLiquidation, cash, positions, open orders parse into atp types.
- **Pass:** equity > 0, fields numeric, no parse errors.
- **Fail:** missing/NaN account fields, parse error.
- **Safety:** halt; treat account as unknown; do not size any order.

## PHASE C — MARKET DATA
- **Input:** subscribe a small fixed set (e.g. AAPL, EUR.USD) via `IBKRMarketFeed`.
- **Expected:** bid/ask/last stream in; each update passes `DataQualityEngine.check_quote`.
- **Pass:** ≥1 clean quote per instrument; no crossed/stale/impossible flags.
- **Fail:** `DATA_NOT_AVAILABLE`, subscription error, persistent stale/crossed quotes.
- **Safety:** `DataQualityEngine` → NO TRADE for that instrument; log; do not fabricate a price.

## PHASE D — HISTORICAL DATA
- **Input:** `IBKRBroker.historical_bars(inst, "1 D", "1 min")` for the test set.
- **Expected:** a list of `Bar`s via `bar_from_ib_historical`.
- **Pass:** monotonic timestamps, positive OHLC, sane volume.
- **Fail:** pacing violation, empty result, malformed bars.
- **Safety:** back off (respect pacing); skip warm-up for that instrument; never invent bars.

## PHASE E — INSTRUMENT MASTER
- **Input:** register the test instruments (`InstrumentSpec`) with contract terms.
- **Expected:** `to_instrument()` keys match the feed/broker keys; underlying families link.
- **Pass:** keys consistent end-to-end; tick/lot rules present.
- **Fail:** key mismatch between master, feed, broker.
- **Safety:** exclude the instrument from the universe until reconciled.

## PHASE F — FEATURE ENGINE
- **Input:** feed real bars into the desk (`on_bar`).
- **Expected:** `FeatureSet` computes (trend/vol/rel_volume/…), `ready` after the slow window.
- **Pass:** finite features; no look-ahead (only bars ≤ now).
- **Fail:** NaN/inf features, features before `ready`.
- **Safety:** treat as not-ready → no signals for that instrument.

## PHASE G — MARKET REGIME
- **Input:** ready features → `RegimeClassifier`.
- **Expected:** a regime label per instrument.
- **Pass:** regime ∈ known set; transitions plausible.
- **Fail:** UNKNOWN persists on ready data / classifier throws.
- **Safety:** default to no directional trading in UNKNOWN/PANIC.

## PHASE H — 9 AI AGENTS
- **Input:** features + regime (+ shared engines: cross-asset/statarb/options/rates/events).
- **Expected:** each active specialist may emit a `Signal`; engine-backed ones only when their data exists.
- **Pass:** signals carry action/confidence/expected_return/stop_distance; no crashes.
- **Fail:** exceptions; signals with impossible fields.
- **Safety:** governance can `suspend` a misbehaving agent; desk skips suspended agents.

## PHASE I — OPPORTUNITY ENGINE
- **Input:** all signals.
- **Expected:** ranked opportunities by score; exits always surfaced.
- **Pass:** deterministic ranking; scores finite.
- **Fail:** ranking error, NaN scores.
- **Safety:** drop malformed opportunities; log.

## PHASE J — MASTER PORTFOLIO MANAGER
- **Input:** ranked opportunities + account.
- **Expected:** allocation decisions honoring budget/positions/diversification; **cash is valid**.
- **Pass:** funded set within gross budget & max positions; correlated duplicates declined.
- **Fail:** allocates beyond budget/limits.
- **Safety:** MPM funds nothing if uncertain (hold cash).

## PHASE K — RISK VETO
- **Input:** each intended order → `RiskEngine.check_order`.
- **Expected:** approvals only within every limit; NO TRADE on any breach.
- **Pass:** every §4 scenario behaves as unit-tested against live account state.
- **Fail:** an order passes that violates a limit.
- **Safety:** **kill switch** — this is the last line; never overridden by the AI.

## PHASE L — PAPER ORDER
- **Input:** a risk-approved order to the **paper** broker.
- **Expected:** order submitted; status transitions observed.
- **Pass:** exactly one order per intent (no duplicates); status SUBMITTED.
- **Fail:** duplicate submission, immediate reject, no status.
- **Safety:** on reject → log + do not retry blindly; on duplicate risk → cancel/`kill_switch`.

## PHASE M — PAPER FILL
- **Input:** await fill.
- **Expected:** fill with price/qty/commission; realistic (not instant free profit).
- **Pass:** fill price near market incl. spread/slippage; commission applied.
- **Fail:** fill at impossible price; missing commission.
- **Safety:** flag as data/exec anomaly; halt if systematic.

## PHASE N — POSITION
- **Input:** fills update the position.
- **Expected:** internal book == broker positions.
- **Pass:** signed qty & avg match the broker.
- **Fail:** position mismatch.
- **Safety:** **STOP TRADING** → reconciliation (Phase Q).

## PHASE O — P&L
- **Input:** mark-to-market from live quotes.
- **Expected:** realized/unrealized P&L update; equity = cash + market value.
- **Pass:** P&L reconciles with fills + marks.
- **Fail:** P&L diverges from account.
- **Safety:** treat account as source of truth; halt on divergence.

## PHASE P — LEARNING RECORD
- **Input:** each round trip → `TradeAssembler` → journal.
- **Expected:** a `TradeRecord` with full §1 fields; journal P&L reconciles with broker.
- **Pass:** record persisted (SQLite/Postgres); MFE/MAE/slippage populated.
- **Fail:** missing record; journal P&L ≠ broker realized.
- **Safety:** journaling never blocks trading; log the discrepancy for review.

## PHASE Q — RECONCILIATION
- **Input:** `Reconciler.run_full(InternalState)` vs broker (positions/cash/orders/P&L).
- **Expected:** consistent report.
- **Pass:** no breaks.
- **Fail:** any position/cash/order/P&L break.
- **Safety:** `RiskEngine.force_halt` (already wired) → stop new trades → recover state → resume only if consistent.

## PHASE R — DASHBOARD
- **Input:** `DashboardContext.snapshot_dict()` served via FastAPI.
- **Expected:** Command Center shows real capital/P&L/positions/risk/agents/health; mode = PAPER; NO DATA where absent.
- **Pass:** values match the engine; emergency-stop trips the kill switch (token-gated).
- **Fail:** any fabricated value; mode unclear; emergency stop ineffective.
- **Safety:** dashboard is read-only; the Risk Engine remains authoritative.

---

## Cross-cutting checks (verify during the phases above)

| Concern | Where handled | Expected behavior |
|--------|----------------|-------------------|
| **Reconnect handling** | `IBKRBroker.ensure_connected` + `disconnectedEvent` | auto-reconnect; while down `RiskEngine.set_broker_connected(False)` → no new orders |
| **Stale market data** | `DataQualityEngine.check_quote` (age) | NO TRADE for that instrument |
| **Subscription errors** | feed error hooks / smoke `DATA_NOT_AVAILABLE` | surfaced, never faked; NO TRADE |
| **API pacing / rate limits** | historical/data requests | back off; keep the requested set small and fixed |
| **Duplicate subscriptions** | one `IBKRMarketFeed` per instrument | avoid re-subscribing the same contract |
| **Duplicate orders** | desk counts `working_qty`; risk check per order | no re-submit of in-flight qty; on suspicion `kill_switch` |
| **Order rejection** | `place_order` status → REJECTED | logged; no blind retry |
| **Broker disconnect** | `set_broker_connected(False)` gate in risk | all new orders blocked |
| **Position mismatch** | `reconcile_full` → `force_halt` | STOP TRADING → reconcile → resume if consistent |
| **Cash mismatch** | `reconcile_full` cash break | as above |
| **Open-order mismatch** | `reconcile_full` order breaks | as above |
| **Emergency kill switch** | `RiskEngine.kill_switch` / dashboard emergency-stop | blocks ALL orders (incl. reductions) until reset |

## Non-negotiables
- No live orders during this plan (paper only).
- No fabricated market data — missing → `DATA_NOT_AVAILABLE`.
- No invented performance — journal/analytics compute only from real fills.
- The Risk Engine is never overridden by the AI model.
