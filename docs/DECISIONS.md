# Architecture Decision Records

Short, honest notes on choices that aren't obvious from the code. Referenced from module
docstrings. Section numbers (§) refer to the Gesamtkonzept V1.0.

## ADR-1 — One code path for backtest and live (§13/§24)

The backtester drives the **same** `AutonomousTradingDesk` used live, replaying historical
bars in chronological order. Entry, sizing, risk and execution all run through the identical
code, so a backtest cannot silently diverge from live behavior, and there is no look-ahead —
the desk only ever sees data up to the current timestamp.

## ADR-2 — Dependency-free core (§25)

The core pipeline and the whole test suite use only the Python standard library (`statistics`,
`random`, `dataclasses`). Broker connectivity, the web API, Redis/Postgres and ML libraries
are **optional extras** (`pip install -e .[live,ml]`). This keeps the offline suite fast,
auditable, and free of hidden numerical dependencies.

`TradingPolicy` deliberately implements the small pydantic-style surface it needs
(`model_copy(update=...)`) itself rather than pulling in pydantic, so `policy.py` has no
third-party import.

## ADR-3 — PaperBroker accounting (§20)

* `cash` reflects every cashflow **and** every commission. Therefore
  `equity = cash + unrealized_pnl` is net of all costs — the honest bottom line, and the
  number the equity curve is built from.
* `realized_pnl` changes **only** when a position is reduced or closed. That lets the
  backtester detect a completed trade simply by watching `realized_pnl` between bars.
* Each close is booked net of *that fill's* commission. The entry commission is already in
  `cash`/`equity`; it is intentionally not double-counted into `realized_pnl`. Consequence:
  the per-trade P&L list is net of exit costs and understates entry cost by one commission,
  while the equity curve is fully net. Documented so the small asymmetry is not a surprise.

## ADR-4 — Risk Engine has absolute veto, and reductions are always allowed (§14)

`RiskEngine.check_order` runs **after** sizing and **before** the broker. Any breach →
`NO TRADE`. The one asymmetry: an order that moves a position **toward flat** is always
approved, even while the desk is halted. You may always cut risk; you may never only add it.

## ADR-7 — Experience journal: fills → trades, independent P&L (§11)

The learning half of the concept (§11/§12/§19) needs every trade stored as an experience.
The broker/desk speak in *fills*; the `TradeAssembler` folds fills into position *episodes*
(entry → flat), emitting one `TradeRecord` per round trip with entry attribution (strategy,
regime, confidence, expected return), path stats (MFE/MAE from marks), slippage and holding
period. This runs the same whether driven by the backtester or the live desk.

Two deliberate points:

* **Independent P&L.** The assembler computes realized P&L itself from the fills, so it is an
  independent check on the broker. A test asserts the journal's P&L reconciles with the
  backtester's own realized-trade P&L (commissions zeroed, because the two use different
  commission conventions — see ADR-3: the broker books only the *exit* commission into
  `realized_pnl`, the assembler books the full round trip).
* **Storage seam.** `TradeJournal` is an interface; `InMemoryJournal` and `SQLiteJournal`
  (stdlib) ship today, a Postgres adapter (§21) implements the same interface later — same
  pattern as the broker abstraction. Journaling on the desk is fully optional (pass a journal
  or not); it changes no trading behavior.

`TradeAnalytics` groups the journal by strategy and regime (expectancy, win-rate, profit
factor, MFE/MAE, and a *calibration* = realized − expected return). Negative calibration is
the first, honest decay signal the governance layer (§19) will later act on.

## ADR-23 — Data-independent backtester frictions (§3/§20)

Built the friction *layer* that doesn't require live market data — models + interfaces,
additive and backward-compatible (defaults reproduce prior behavior, so the whole suite still
passes).

* **Cost models** (`atp.costs`): `CommissionModel` (per-share/per-contract/percent),
  `SlippageModel` (fixed/spread/volume), `FinancingModel` + `BorrowModel` (flat or rate-table),
  `FXConverter`. Rates/FX come from **injected sources** — a missing FX rate returns `None`
  (DATA_NOT_AVAILABLE), never a fabricated number. `PaperBroker` accepts optional
  `commission_model`/`slippage_model`; without them its behavior is byte-for-byte unchanged.
* **Session/holiday gating** (§3): the desk takes an optional `MarketCalendar`; when closed
  (outside hours or on a holiday) it does not trade. Wired through the Backtester and
  `build_paper_stack`. No calendar → prior policy-hours behavior.
* **Corporate actions / rollover / expiry** (`atp.corpactions`): `Split`/`Dividend` +
  `CorporateActionsProcessor` (value-neutral split adjust, signed dividend cash, via new
  `PaperBroker.adjust_position`/`credit_cash` primitives); `FuturesRoll`/`RollCalendar` +
  processor (flatten old, open next); an options-expiry discovery helper over the existing
  `settle_expiration`. **All action/roll data is caller-supplied — nothing is invented.** An
  empty book/calendar applies nothing.

What remains is purely feeding these with real reference data (exchange calendars, corp-action
feeds, roll schedules, FX rates) — which comes with the broker/data vendor.

## ADR-22 — Trading-brain completion: master, risk, data-quality, instruments, scanner, IBKR

Round focused on the broker-independent gaps needed before real market data connects.

* **Instrument Master** (`atp.instruments`) — the unified reference model: `InstrumentSpec`
  (id/venue/contract terms/margin/liquidity/derivative terms) + `InstrumentMaster` that
  understands **underlying relationships** (a GOLD family: spot/future/ETF/option), plus a
  `MarketCalendar` (hours/holidays). `core.events.Instrument` stays the hot-path value object.
* **Data Quality Engine** (`atp.dataquality`) — a central NO-TRADE gate: stale, missing,
  impossible price, crossed/abnormal spread, duplicate/non-monotonic/future timestamp,
  impossible jump, feed disconnect, heartbeat silence. Wired into the desk's data gate.
* **Risk Engine hardened** (`atp.risk`) — added correlated-cluster exposure, an emergency
  **kill switch** (blocks even reductions), a **broker-disconnect** gate, and an
  **invalid-price** guard, with a strict precedence order. Every §4 scenario is now an
  automated test (`test_risk_scenarios.py`) — the veto is proven, and the AI can never
  override it.
* **Master Portfolio Manager** (`atp.portfolio`) — a distinct allocator between ranking and
  execution: portfolio budget (gross headroom), max positions, per-name cap, and correlation
  diversification (decline a same-direction highly-correlated duplicate). Cash is a valid
  outcome. Wired into the desk optionally.
* **Market Universe Scanner** (`atp.scanner`) — the hierarchical funnel (liquidity → volatility
  → momentum/anomaly → rank) that narrows the global universe to the shortlist worth deep
  real-time data. Input is caller-supplied summaries; no fabricated data.
* **IBKR adapter prepared** (`atp.brokers.ibkr`) — added historical bars, executions, open
  orders, reconnect (`ensure_connected`) and error/disconnect hooks (real lazy `ib_async`,
  live-only), with pure `bar_from_ib_historical` mapper unit-tested. `docs/IBKR_SETUP.md`
  documents exactly the credentials, ports and market-data subscriptions needed tomorrow.
* **Docker** — `Dockerfile` + `docker-compose.yml` (Postgres journal + Redis state + app; IB
  Gateway stays on the host).

All additive: default construction is unchanged, so every prior test still passes. No synthetic
data is ever presented as trading performance (§14) — it appears only in unit/integration tests.

## ADR-21 — Application layer: config-driven assembly + CLI (§15/§24)

The capstone that turns the library into one program. `SystemConfig` captures the single set of
decisions the desk runs on (§15) — capital, risk limits, enabled specialists, regime and
execution settings — and projects them onto the runtime (`TradingPolicy`, strategy list,
execution knobs); it round-trips through JSON so a deployment is configured once. `app.py`
assembles a `Backtester` from a config and runs it, capturing trades in a journal.
`python -m atp` exposes `version` / `config` / `backtest`, so the whole pipeline runs from one
command.

Honest boundary made explicit in code: only the **feature-only** specialists (momentum,
mean-reversion, breakout) are configurable by name; engine-backed ones (cross-asset, stat-arb,
volatility, fx-carry, macro, event) need their shared data engines and are wired
programmatically — `build_strategies` raises a clear error rather than silently dropping them.

## ADR-20 — Multi-leg option execution & settlement (§16/§5)

Options trade as *combos*, not single legs. `Combo`/`OptionLeg` model a structure (verticals,
straddles, strangles ship as builders); `net_debit` and `greeks` aggregate the legs via the
Black–Scholes layer (signed by side), so a spread's cost and its position greeks are exact.
`execute_combo` works each leg against the broker and reports the net cashflow (negative =
debit paid). `settle_expiration` cash-settles every expired option position to intrinsic value
via a synthetic quote, realizing option P&L in the broker.

Verified end to end: a 5-lot 100/110 call spread pays a ~$1.46k debit and, settled at a spot of
112 (both legs ITM, 10 wide), realizes exactly `5 × 1000 − debit`.

**Physical assignment** (`style="physical"`) is now implemented too: for in-the-money legs the
option is booked at intrinsic *and* the underlying share position is established at spot —
long call → long stock, long put → short stock, short call assigned → short stock, short put
assigned → long stock. Modeling it as "settle the option + take stock at market" is
economically identical to exercise (the option's P&L realizes, the stock enters at market with
zero unrealized) and keeps the broker book consistent — a test checks equity is preserved
(e.g. a long call exercised at 110 having cost 3 leaves equity = start + $700 and 100 long
shares). OTM legs still expire worthless with no assignment.

## ADR-19 — Time-scheduled execution: TWAP / VWAP (§16)

`SlicingAlgo` splits an order within one step; the `ExecutionScheduler` works a parent order
*across bars*, releasing one child slice per tick — TWAP (equal slices) or VWAP (weighted by a
volume profile). Each slice still passes the Risk Engine; a vetoed/unfilled slice aborts the
working order (risk said no — stop working it).

Desk integration (opt-in via `execution_slices` / `vwap_profile`): the desk ticks the scheduler
at the top of each step to release the next slice (journaling fills with the entry context it
stored), and — crucially — counts the in-flight `working_qty` against its target so it doesn't
re-submit what's already being worked. Exits and risk-reducing orders bypass the scheduler and
execute immediately (and cancel any working entry on that instrument first) — you unwind now,
you don't work an exit slowly. An integration test confirms an entry builds up over multiple
bars and the journal still reconciles with the broker. Without the config, nothing changes.

Remaining execution refinement (named, not stubbed): scheduling that adapts child size to
*live* volume/urgency intra-schedule, and completing any residual working order at end-of-run.

## ADR-18 — Pluggable context feeds (§5/§17)

The shared context engines (`OptionsEngine`, `RatesTable`, `EconomicCalendar`) were fed by hand
in tests/demos. `ContextFeed` is the seam that keeps them current from external data: each feed
pulls its slice and pushes it into its engine on `refresh(now)`, returning an update count. A
`FeedHub` refreshes them together and **isolates failures** (one bad feed can't stop the rest).
The `LiveRunner` calls the hub each bar (configurable), before strategies read the context — so
options/rates/events stay live alongside market data.

Offline/reference implementations ship and are tested: `ScheduledRatesFeed` (applies dated rate
changes as time passes), `ScheduledEventsFeed` (schedules an event, then reveals its `actual`
once its timestamp passes — `EconomicCalendar.add` is now an upsert by ts+kind for this), and
`OptionsChainFeed` (rebuilds a chain from a spot callback). A production adapter swaps only the
data source behind the same `refresh` contract — the desk and strategies don't change. An
integration test shows the runner evolving a rate schedule mid-run and the macro strategy
trading the resulting easing bias.

This is the last structural seam: with it, plugging a real market-data / options / rates /
events / gateway provider is an adapter, not a rewrite.

## ADR-17 — Event specialist & economic calendar (§5/§8) — 9 of 9 specialists

The last of the nine §8 specialists. An `EconomicCalendar` holds scheduled events per
instrument (earnings/CPI/FOMC) with importance and, once released, an expected/actual pair (the
*surprise*). The `EventStrategy` does two things: **flatten into** a high-impact event (emit
CLOSE so no directional bet rides a binary print), and **trade the surprise** afterwards (beat
=> buy, miss => sell) within a reaction window. It fires only on state transitions
(idle/blackout/react), so once per event edge. Fed by an events feed in production; populated
directly in tests/demos.

With this, all nine specialists named in §8 exist — Momentum, Mean-Reversion, Macro, Options
(Volatility), Commodities-capable, FX (Carry), Statistical Arbitrage, Event and Cross-Asset —
each a transparent, tested model, and each honest about the data it needs.

## ADR-16 — Macro rates layer: FX carry & macro cycle (§5/§8)

Adds the macro/rates data ebene (§5) and the two rate-driven specialists (§8), completing 8 of
the 9 named specialists.

* **RatesTable** — per-currency policy rates with history, exposing `carry(base, quote)` (the
  rate differential) and `trend(ccy)` (hiking vs. cutting). Fed by a macro feed in production;
  set directly in tests/demos. Pure stdlib.
* **FXCarryStrategy** — the carry trade: long the higher-yielder funded in the lower-yielder
  when carry is meaningfully positive, short when negative, with a price-trend gate so it
  doesn't hold a positive-carry long straight into a strong downtrend (how carry unwinds hurt).
  Stands aside in panic.
* **MacroStrategy** — a rate-cycle bias: easing (falling policy rate) → risk-on long bias on
  that currency's equities/indices; tightening → step out / lean short.

Both are transparent, clearly-labeled heuristics over shared macro state — not claims that
rates mechanically set prices; they only act on a formed carry/cycle signal. Still open (needs
a richer data feed): an economic-events *calendar* (earnings/CPI/central-bank dates) to size
down into events — the last of the 9 specialists, Event, depends on it.

## ADR-15 — Options & volatility data layer (§5/§8/§17)

Adds the derivative data ebene the concept lists (§5), exact and offline-tested:

* **Instrument** gains optional `expiry`/`strike`/`right`/`underlying`; `key` stays unchanged
  for non-derivatives and becomes unique per option. The IBKR adapter now maps **futures and
  options** via a pure `contract_spec` (ib_async-free, tested) — closing the ADR-6 gap. (The
  futures *exchange* is left for IB to qualify; a fuller impl carries the venue.)
* **Black–Scholes** pricing, Greeks (Δ/Γ/vega/θ/ρ) and an **implied-vol** solver (Newton +
  bisection), pure `math`, verified against textbook values and put–call parity. `implied_vol`
  returns `None` below intrinsic / out of range rather than fabricating a number.
* **Option chain analytics** (`compute_features`): ATM IV, volatility skew, put/call OI &
  volume ratios, and a dealer gamma-exposure proxy. `build_chain` synthesizes a skewed chain
  from spot + base IV using the greeks, so the whole stack is testable without a live feed.
* **OptionsEngine** holds latest chain features + a rolling ATM-IV history for an **IV rank**.
  The **VolatilityStrategy** reads it and fades option-priced fear (high IV rank + put-heavy
  flow) on the *underlying*.

Honest boundaries: the vol specialist trades the underlying on options *sentiment* — it is a
labeled heuristic, not a claim that IV predicts direction, and gated on genuine extremes.
Direct multi-leg options *execution* (spreads, assignment) and a real options-data feed are
named next steps, not stubbed as if present.

## ADR-14 — More specialists: Breakout and StatArb pairs (§8)

Two more of the nine §8 specialists, both from price/volume (no new data feeds):

* **BreakoutStrategy** — trades range breakouts *confirmed by volume* (high |z| on above-average
  relative volume), active only in breakout/trend/high-vol regimes. Pure feature-based; no new
  wiring.
* **StatArbStrategy** + `StatArbEngine` — a real two-leg pairs trade. The engine tracks the OLS
  hedge ratio `β`, the spread `price_a − β·price_b`, its z-score, and the legs' returns
  correlation (a "relationship intact" gate). Expressed through the per-instrument signal
  interface: the engine is **shared state** fed each bar via a new generic desk `observers`
  hook, and the strategy returns the correct leg direction for whichever instrument it's asked
  about — so the desk fires SELL(rich) + BUY(cheap) in the same step.

  **β-weighted market-neutral sizing** (added): a signal `sizing="hedged"` mode makes both legs
  derive units from a *common reference price* (leg A) and a hedge factor — leg A gets base
  units, leg B gets `β·base`, so `qty_b = β·qty_a`. This neutralizes the shared move regardless
  of price levels/intercept (equal-notional does not, when β ≠ price ratio). A test asserts the
  pair nets ~0 P&L on a hedge-consistent shared move, and the demo — on a cleanly cointegrated
  pair — is profitable, capturing only the spread reversion. Note the real-world caveat: hedge-
  ratio *estimation error* leaves residual exposure, so StatArb needs the tradable spread to be
  meaningful next to the shared volatility.

Not built (need data the platform doesn't yet model, so not faked): Macro / FX-carry (rate
differentials), Options (chains/greeks/IV), Event (news/earnings calendar). These are named
gaps, not silent stubs.

## ADR-13 — Smart execution: convex impact, slicing, urgency (§16)

Execution grew from "submit one market order" into a real §16 layer, while staying additive
(defaults reproduce the old behavior, so the existing suite is untouched).

* **Market-impact model** (`MarketImpactModel`, square-root law): adverse bps as a function of
  order size vs. average volume. Wired into `PaperBroker` fills (opt-in via `impact_model` +
  `set_liquidity`), so fills are size-aware — bigger orders fill worse.
* **Execution algos.** `ImmediateAlgo` (default, one order) and `SlicingAlgo` (split by
  participation cap). Because impact is *convex* in size, working a large order as N slices
  costs ~1/√N of the one-shot impact — demonstrated by a backtest test and demo where slicing
  ends with higher equity on identical signals. Urgent / risk-reducing (close) orders skip
  slicing and execute immediately: cutting risk fast beats saving a few bps.
* **Aggregation.** The engine risk-checks the *parent* once, works the children, then folds
  their fills into one parent-level result (size-weighted price, summed commission) — so the
  journal and desk see a single trade, unchanged. The desk feeds a liquidity reference (recent
  volume) and an urgency flag; without them, nothing changes.

`SlicingAlgo` works children within the step (enough to model the convex-impact saving
deterministically); time-scheduled VWAP/TWAP remains a further extension.

## ADR-12 — Cross-Asset Intelligence: shared state via a fed engine (§6/§8)

The concept's "Global Market Brain" (§4/§6) reads relationships *between* markets. A
`CrossAssetEngine` tracks configured `Relationship(leader, follower, expected_sign)`s over a
rolling window: realized correlation, the follower move *implied* by the leader (a
correlation-scaled regression), and the normalized `divergence_z` (how far the follower has
decoupled). Confirmation vs divergence falls straight out of those two numbers.

Wiring choice: cross-asset context is multi-instrument, but `Strategy.generate` is per
instrument. Rather than change that contract, the engine is **shared state** — the desk feeds
it every bar (`on_bar`), and `CrossAssetStrategy` holds a reference to the *same* engine and
keys off the follower instrument it's asked about. So the specialist fits the existing
interface while still seeing the whole cross-asset picture. The engine is optional and
additive everywhere (desk / Backtester / `build_paper_stack`): no engine, and nothing changes.
Pure stdlib maths, fully offline-tested.

## ADR-11 — Dashboard, persistence, and the live-gateway boundary (§21/§22/§17)

* **Dashboard read-model.** `build_snapshot()` assembles one JSON-able view (account,
  positions, risk, per-instrument regimes, journal edge, governance) from the live objects. It
  is pure and tested; `api.py` (`create_app`, lazy FastAPI) is a thin, **read-only** serializer
  over it, and the bundled `static/index.html` renders exactly that shape — falling back to a
  bundled `snapshot.json` so it works as a static file too. The production frontend (Next.js,
  §22) consumes the same endpoint. The API observes; it never places or cancels orders.
* **Persistence seams.** `StateStore` (fast, ephemeral realtime state, §21) ships as
  `InMemoryStateStore` (tested, JSON-serialized to match Redis semantics) and `RedisStateStore`
  (lazy `redis`). The durable journal (§11) gains `PostgresJournal` — same `TradeJournal`
  interface and column order as `SQLiteJournal`, lazy `psycopg`, so records round-trip
  identically and the offline suite proves the logic via SQLite. Operational state (overwritten
  each cycle) is deliberately kept separate from the append-only journal (system of record).
* **Live-gateway boundary (honest).** The IBKR broker/feed mapping is unit-tested, but a real
  socket, live market data and real fills cannot be exercised offline. `examples/ibkr_smoke.py`
  is the operator's validation tool: it connects to a running IB Gateway, prints account +
  positions, reconciles, and only places an order behind an explicit `--place-test-order` flag
  (read-only by default, paper account). CI never runs it; it touches a real broker.

## ADR-10 — Live runtime: same desk, feed seam, in-loop governance (§17/§19)

The `LiveRunner` drives the **same** `AutonomousTradingDesk` as the backtester, off a
`MarketFeed` instead of a historical replay — so paper/live cannot diverge from backtest
(§13). Each bar it feeds the desk, steps it, and books the fills into an internal ledger.
Periodically it (a) reconciles that ledger against the broker (§17) and (b) runs the
governance monitor over the journal (§19), so a decaying strategy is suspended *mid-stream*
and the desk stops acting on it — the learning loop, live.

* **Feed seam.** `ReplayFeed` (offline, deterministic, drives paper trading and the tests) and
  `IBKRMarketFeed` (live, lazy `ib_async`) implement one interface. The live event bridge is
  live-only, but the IB→atp mapping (`bar_from_rt`, `quote_from_ticker`) is pure and unit
  tested — same honesty boundary as the broker adapter (ADR-6).
* **Broker-agnostic, one exception.** The runner forwards quotes to a `PaperBroker` so it can
  fill; a real broker fills from the market. Everything else is identical across paper and
  live — swapping `ReplayFeed`/`PaperBroker` for `IBKRMarketFeed`/`IBKRBroker` changes nothing
  in the desk (§3). `build_paper_stack` assembles the paper stack so callers don't repeat it.
* **Reconciliation is real here.** The internal book is built independently from executed
  fills, then compared to broker positions; a mismatch trips `RiskEngine.force_halt`. In paper
  they agree (a live consistency proof); in production drift is caught.

## ADR-9 — Strategy Discovery: enumerate candidates, validate honestly (§12/§13)

Discovery generates a *family* of candidate strategies (`RuleStrategy`: a threshold rule on
one feature, optionally filtered) from an explicit `SearchSpace`, and runs each through the
mandatory gauntlet — in-sample → **out-of-sample** → **walk-forward** → **Monte-Carlo** —
accepting only those that clear every gate on data they were not "selected" on. The feature
vocabulary is an auditable accessor table, not raw attribute access, so the search space is a
known, reviewable set of signals (options-flow / cross-asset / news plug in there later).

Two integrity points:

* **No leakage across runs.** A candidate is a stateful object reused across many runs
  (in-sample, OOS, each walk-forward window). `Strategy.reset()` — now called at the start of
  every `Backtester.run()` — clears per-instrument state so runs are independent (§13). This
  also made existing backtests robustly reproducible regardless of instance reuse.
* **Selection bias is surfaced, not hidden.** Trying many rules and keeping the best inflates
  apparent edge. `DiscoveryResult` reports how many candidates were tried and prints an
  explicit multiple-testing caveat; acceptance is judged only on OOS + walk-forward +
  Monte-Carlo, and paper trading remains the real final gate (§12). Survivors are handed to
  the `ModelRegistry` as versioned, governed models (§19) — never trusted as proof.

## ADR-8 — Governance: journal-driven suspension, validated promotion (§19)

Governance closes the learning loop with two mechanisms, both driven only by recorded data:

* **StrategyRegistry** is the live on/off switch. The desk calls `is_active(name)` before
  acting on a strategy's signals; a `GovernanceMonitor` reads the journal (§11) and, on a
  large-enough sample, suspends a strategy that breaches `DecayPolicy` (negative expectancy,
  profit factor < 1, weak calibration, …). Optional probation-first escalation and
  auto-reactivation on recovery. An unregistered strategy defaults to active, so governance
  is strictly additive — a desk with no registry behaves exactly as before.
* **ModelRegistry** versions models and promotes a candidate *only* after a validated
  improvement in its out-of-sample primary metric (default Sharpe) by a margin; otherwise the
  incumbent stands. `rollback()` restores the previous promoted version on decay. No live
  model is ever swapped on an unvalidated promise (§25).

Both are consulted/updated out-of-band (like reconciliation), keeping the hot path — the desk
step — unchanged except for the cheap `is_active` gate.

## ADR-6 — IBKR adapter: injected client seam, honest offline boundary (§17)

The IBKR adapter (`atp.brokers.ibkr`) is deliberately thin. The logic that can actually be
*wrong* — mapping an `atp` `Order`/`Instrument` onto an IB order/contract, and parsing IB's
account/position/fill objects back — is isolated behind two injected seams (`ib` client and
`factory`) and a pure `describe_order()` function. That whole surface is unit-tested with a
`FakeIB` mirroring the `ib_async` methods used, so it needs neither a live gateway nor the
`ib_async` dependency (which is lazy-imported only inside `connect()`/`IBFactory`).

What the offline suite does **not** prove: a real socket connection, live market data, and
real fills. Those require a running IB Gateway (paper account, port 4002) and are validated
in paper trading (§24 Phase 14), not in CI. Futures/options contract mapping is intentionally
unimplemented (raises) because the current `Instrument` carries no expiry/strike — a visible
gap, not a silent guess.

Reconciliation (`atp.brokers.reconcile`, §17) compares the desk's internal book against the
broker's positions each cycle; any break trips `RiskEngine.force_halt`, which stops new risk
while still permitting reductions so the desk can flatten toward broker truth.

## ADR-5 — Modeled frictions vs. extension points (§13/§16)

Modeled today: commission (per-unit + minimum), bid/ask spread (via the quote), and slippage
(bps, adverse). Simplified/stubbed for now and marked as extension points: latency,
market-impact curves, partial fills, and smart execution algorithms (VWAP/TWAP). They are
not hidden assumptions — they are named here so nobody mistakes the current fidelity for the
target fidelity.
