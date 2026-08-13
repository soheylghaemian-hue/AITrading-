import React from "react";
import type { Snapshot } from "../lib/types";
import { Pill, Section } from "./sections";
import { money, num, pct, price, spread, sign, hhmmss, isPresent, NO_DATA } from "../lib/format";

function Empty({ label = NO_DATA }: { label?: string }) {
  return <div className="empty">{label}</div>;
}

/* ---------------------------------------------------------------- watchlist (rich market data) */
export function Watchlist({ s }: { s: Snapshot | null }) {
  const md = s?.market_data ?? [];
  return (
    <Section title="Live market · watchlist"
      right={<span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{md.length} instruments</span>}>
      <div className="wrap">
        {md.length === 0 ? <Empty label={s ? "MARKET DATA UNAVAILABLE" : NO_DATA} /> : (
          <table>
            <thead><tr>
              <th>Symbol</th><th>Price</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Bid Sz</th><th>Ask Sz</th>
              <th>Volume</th><th>Change</th><th>Type</th><th>Quality</th><th>Time</th><th>Status</th><th>Subscription</th>
            </tr></thead>
            <tbody>
              {md.map((m) => {
                const avail = m.status === "DATA_AVAILABLE";
                const quality = m.status === "DATA_AVAILABLE" ? "ok"
                  : m.status === "DELAYED" ? "delayed" : m.status === "STALE" ? "stale" : "no_data";
                return (
                  <tr key={m.symbol}>
                    <td>{m.symbol}</td>
                    <td>{price(m.last ?? m.bid)}</td>
                    <td>{price(m.bid)}</td>
                    <td>{price(m.ask)}</td>
                    <td>{spread(m.bid, m.ask)}</td>
                    <td>{isPresent((m as any).bid_size) ? num((m as any).bid_size, 0) : "—"}</td>
                    <td>{isPresent((m as any).ask_size) ? num((m as any).ask_size, 0) : "—"}</td>
                    <td>{isPresent((m as any).volume) ? num((m as any).volume, 0) : "—"}</td>
                    <td>{isPresent((m as any).change) ? pct((m as any).change) : "—"}</td>
                    <td>{m.market_data_type ? <Pill text={m.market_data_type} /> : "—"}</td>
                    <td><Pill text={quality} /></td>
                    <td>{hhmmss(m.timestamp)}</td>
                    <td><Pill text={m.status} /></td>
                    <td className="reason">{avail ? "active" : (m.error_code ? `IBKR ${m.error_code} — ${m.reason ?? ""}` : (m.reason ?? ""))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- opportunity center */
// A signal may only be shown as tradable when its instrument has valid (available) market data.
function availableSymbols(s: Snapshot | null): string[] {
  return (s?.market_data ?? []).filter((m) => m.status === "DATA_AVAILABLE").map((m) => m.symbol);
}
function tradable(instrument: string, avail: string[]): boolean {
  const base = instrument.split(/[:.]/)[0].toUpperCase();
  return avail.some((sym) => sym.toUpperCase().replace(/[^A-Z0-9]/g, "").startsWith(base));
}

export function Opportunities({ s }: { s: Snapshot | null }) {
  const signals = (s?.ai_analysis ?? []).filter((o) => o.status === "SIGNAL" || o.status === "OBSERVATION");
  const avail = availableSymbols(s);
  return (
    <Section title="Opportunity center"
      right={<span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{signals.filter(o => o.status === "SIGNAL").length} signals</span>}>
      <div className="wrap">
        {signals.length === 0 ? <Empty label={s ? "NO OPPORTUNITIES" : NO_DATA} /> : (
          <table>
            <thead><tr>
              <th>Instrument</th><th>Direction</th><th>Strategy</th><th>Confidence</th><th>Exp. move</th>
              <th>Entry</th><th>Stop</th><th>Target</th><th>R/R</th><th>Data quality</th><th>Status</th>
            </tr></thead>
            <tbody>
              {signals.map((o, i) => {
                const ok = tradable(o.instrument, avail);
                const status = !ok ? "NOT AVAILABLE" : o.status === "SIGNAL" ? "ANALYZING" : "DISCOVERED";
                return (
                  <tr key={i}>
                    <td>{o.instrument}</td>
                    <td>{o.action ?? "—"}</td>
                    <td>{o.agent}</td>
                    <td>{o.confidence == null ? "—" : pct(o.confidence)}</td>
                    <td>{isPresent(o.expected_return) ? pct(o.expected_return) : "—"}</td>
                    <td>NO DATA</td><td>NO DATA</td><td>NO DATA</td><td>NO DATA</td>
                    <td><Pill text={ok ? "ok" : "no_data"} /></td>
                    <td><Pill text={status} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <div className="banner" style={{ borderTop: "1px solid var(--border)", borderBottom: "none" }}>
        Signals are read-only observations. Entry/stop/target/R-R require a backend opportunity
        endpoint (not yet in the read-model) — shown as NO DATA, never fabricated. An instrument
        without valid market data is flagged and never presented as a tradable trade.
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- trade journal */
export function TradeJournal({ s }: { s: Snapshot | null }) {
  const trades = s?.recent_trades ?? [];
  const cell = (v: any, f: (x: any) => string = (x) => String(x)) => (v == null ? "—" : f(v));
  return (
    <Section title="Trade journal · learning records"
      right={<span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{s?.n_trades ?? 0} trades</span>}>
      <div className="wrap">
        {trades.length === 0 ? <Empty label={s ? "NO TRADES" : NO_DATA} /> : (
          <table>
            <thead><tr>
              <th>Exit</th><th>Instrument</th><th>Agent</th><th>Action</th><th>Qty</th><th>Entry</th><th>Exit</th>
              <th>Realized P&L</th><th>MFE</th><th>MAE</th><th>Slippage</th><th>Regime</th><th>Model</th><th>Outcome</th>
            </tr></thead>
            <tbody>
              {trades.map((t: any, i: number) => (
                <tr key={t.trade_id ?? i}>
                  <td>{hhmmss(t.exit_ts)}</td>
                  <td>{(t.instrument_key ?? "").split(":")[0] || "—"}</td>
                  <td>{t.agent ?? t.strategy ?? "—"}</td>
                  <td>{t.signal_action ?? t.direction ?? "—"}</td>
                  <td>{cell(t.quantity, (x) => num(x, 0))}</td>
                  <td>{cell(t.entry_price, price)}</td>
                  <td>{cell(t.exit_price, price)}</td>
                  <td className={sign(t.realized_pnl)}>{cell(t.realized_pnl, (x) => money(x))}</td>
                  <td>{cell(t.mfe, (x) => money(x))}</td>
                  <td>{cell(t.mae, (x) => money(x))}</td>
                  <td>{cell(t.slippage ?? t.execution_slippage, (x) => num(x, 4))}</td>
                  <td>{t.regime ?? t.market_regime ?? "—"}</td>
                  <td>{t.model_version ?? t.strategy_version ?? "—"}</td>
                  <td>{t.result ? <Pill text={t.result} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- performance + equity curve */
function EquityCurve({ trades }: { trades: any[] }) {
  const pnls = trades.map((t) => t.realized_pnl).filter(isPresent) as number[];
  if (pnls.length === 0) return <Empty label="NO DATA · no closed trades yet" />;
  const chrono = [...pnls].reverse(); // recent_trades is newest-first
  let cum = 0; const pts = chrono.map((p) => (cum += p));
  const min = Math.min(0, ...pts), max = Math.max(0, ...pts), rng = max - min || 1;
  const W = 600, H = 120, n = pts.length;
  const path = pts.map((v, i) => `${(i / Math.max(1, n - 1)) * W},${H - ((v - min) / rng) * H}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <div style={{ padding: "14px 16px" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="120" preserveAspectRatio="none">
        <line x1="0" y1={H - ((0 - min) / rng) * H} x2={W} y2={H - ((0 - min) / rng) * H} stroke="var(--border2)" strokeWidth="1" />
        <polyline points={path} fill="none" stroke={last >= 0 ? "var(--green)" : "var(--red)"} strokeWidth="2" />
      </svg>
      <div className="mono" style={{ color: "var(--muted2)", fontSize: 11, marginTop: 6 }}>
        cumulative realized P&L over {n} closed trades · latest {money(last, 0)}
      </div>
    </div>
  );
}

export function PerformanceFull({ s }: { s: Snapshot | null }) {
  const o: any = s?.analytics_overall ?? {};
  const trades = s?.recent_trades ?? [];
  const noTrades = !s || (s.n_trades ?? 0) === 0;
  const row = (label: string, value: string, cls = "") => (
    <div className="h"><span className="n">{label}</span><span className={`mono ${cls}`}>{value}</span></div>
  );
  return (
    <Section title="Performance">
      {noTrades ? <Empty label="NO DATA · performance appears only after real closed trades" /> : (
        <>
          <EquityCurve trades={trades} />
          <div className="health">
            {row("Win rate", pct(o.win_rate))}
            {row("Profit factor", num(o.profit_factor))}
            {row("Expectancy", money(o.expectancy))}
            {row("Avg winner", money(o.avg_win))}
            {row("Avg loser", money(o.avg_loss))}
            {row("Total P&L", money(o.total_pnl, 0), sign(o.total_pnl))}
            {row("Max drawdown", pct(o.max_drawdown))}
            {row("Sharpe", isPresent(o.sharpe) ? num(o.sharpe) : NO_DATA)}
            {row("Sortino", isPresent(o.sortino) ? num(o.sortino) : NO_DATA)}
            {row("Trades", num(s?.n_trades, 0))}
          </div>
        </>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- learning engine */
const LIFECYCLE = ["research", "testing", "paper", "approved", "live", "suspended", "retired"];
export function LearningEngine({ s }: { s: Snapshot | null }) {
  const g = s?.governance ?? [];
  return (
    <Section title="Learning engine · model lifecycle">
      <div className="wrap">
        {g.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Model / strategy</th><th>Version</th><th>Lifecycle</th><th>Decay / reason</th><th>Since</th></tr></thead>
            <tbody>
              {g.map((x: any) => (
                <tr key={x.name}>
                  <td>{x.name}</td>
                  <td>{x.version ?? "—"}</td>
                  <td><Pill text={x.status} /></td>
                  <td className="reason">{x.reason || "—"}</td>
                  <td>{hhmmss(x.since)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="funnel">
        {LIFECYCLE.map((stage) => {
          const count = g.filter((x: any) => (x.status || "").toLowerCase() === stage).length;
          return <div className="step" key={stage}><div className="n">{s ? count : NO_DATA}</div><div className="t">{stage}</div></div>;
        })}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- exposure breakdown */
export function Exposure({ s }: { s: Snapshot | null }) {
  const a: any = s?.account ?? {};
  const p = s?.positions ?? [];
  const long = p.filter((x: any) => x.side === "long").reduce((z, x: any) => z + (x.notional || 0), 0);
  const short = p.filter((x: any) => x.side === "short").reduce((z, x: any) => z + (x.notional || 0), 0);
  const byClass: Record<string, number> = {};
  for (const x of p as any[]) byClass[x.asset_class ?? "?"] = (byClass[x.asset_class ?? "?"] || 0) + (x.notional || 0);
  const row = (label: string, value: string) => (
    <div className="h"><span className="n">{label}</span><span className="mono">{value}</span></div>
  );
  return (
    <Section title="Portfolio exposure">
      {!s ? <Empty /> : (
        <div className="health">
          {row("Gross exposure", money(a.gross_exposure, 0))}
          {row("Net exposure", money(a.net_exposure, 0))}
          {row("Long exposure", money(long, 0))}
          {row("Short exposure", money(short, 0))}
          {row("Gross leverage", isPresent(s.risk?.gross_leverage as any) ? num(s.risk?.gross_leverage as any) + "×" : NO_DATA)}
          {Object.keys(byClass).length === 0
            ? row("By asset class", p.length === 0 ? "flat" : NO_DATA)
            : Object.entries(byClass).map(([k, v]) => row(`Exposure · ${k}`, money(v, 0)))}
          {row("Currency exposure", NO_DATA)}
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- tradeable universe */
export function TradeableUniverse({ s }: { s: Snapshot | null }) {
  const u = s?.tradeable_universe ?? [];
  const nTradeable = u.filter((x) => x.tradeable).length;
  return (
    <Section title="Tradeable universe · data-quality gate" right={
      <span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{nTradeable}/{u.length} tradeable</span>}>
      <div className="wrap">
        {u.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Symbol</th><th>Asset</th><th>Exchange</th><th>State</th><th>Data type</th>
              <th>Last valid</th><th>IBKR error</th><th>Reason</th></tr></thead>
            <tbody>
              {u.map((x) => (
                <tr key={x.symbol}>
                  <td>{x.symbol}</td><td>{x.asset_class ?? "—"}</td><td>{x.exchange ?? "—"}</td>
                  <td><Pill text={x.tradeable ? "tradeable" : "blocked"} /></td>
                  <td>{x.data_type ? <Pill text={x.data_type} /> : "—"}</td>
                  <td>{hhmmss(x.last_valid_timestamp)}</td>
                  <td>{x.ibkr_error ?? "—"}</td>
                  <td className="reason">{x.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="banner ok" style={{ borderTop: "1px solid var(--border)" }}>
        Only REALTIME · DATA_AVAILABLE · valid-price instruments are tradeable. Blocked instruments
        never enter the opportunity pipeline — the autonomous engine cannot generate an executable
        trade for them (no stale/delayed/fabricated prices). Existing positions are governed by a
        separate risk policy and are not auto-closed on a temporary data loss.
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- global market data grid (§ Phase 10) */
export function GlobalMarketData({ s }: { s: Snapshot | null }) {
  const g = s?.global_market_data ?? [];
  const nReady = g.filter((x) => x.status === "READY").length;
  return (
    <Section title="Global market data · provider-independent grid" right={
      <span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{nReady}/{g.length} realtime</span>}>
      <div className="wrap">
        {g.length === 0 ? <Empty label={s ? "MARKET DATA UNAVAILABLE" : NO_DATA} /> : (
          <table>
            <thead><tr>
              <th>Region</th><th>Exchange</th><th>Symbol</th><th>Source</th><th>Status</th><th>RT</th>
              <th>Bid</th><th>Ask</th><th>Last</th><th>Spread</th><th>Bid Sz</th><th>Ask Sz</th>
              <th>Volume</th><th>Latency</th><th>Time</th><th>Subscription</th><th>Detail</th>
            </tr></thead>
            <tbody>
              {g.map((x) => (
                <tr key={`${x.region}-${x.symbol}`}>
                  <td>{x.region}</td><td>{x.exchange ?? "—"}</td><td>{x.symbol}</td>
                  <td>{x.source ?? "—"}</td>
                  <td><Pill text={x.status} /></td>
                  <td>{x.realtime ? "✓" : "—"}</td>
                  <td>{price(x.bid)}</td><td>{price(x.ask)}</td><td>{price(x.last)}</td>
                  <td>{spread(x.bid, x.ask)}</td>
                  <td>{isPresent(x.bid_size) ? num(x.bid_size, 0) : "—"}</td>
                  <td>{isPresent(x.ask_size) ? num(x.ask_size, 0) : "—"}</td>
                  <td>{isPresent(x.volume) ? num(x.volume, 0) : "—"}</td>
                  <td>{isPresent(x.latency_ms) ? `${Math.round(x.latency_ms as number)} ms` : "—"}</td>
                  <td>{hhmmss(x.timestamp)}</td>
                  <td><Pill text={x.subscription_state} /></td>
                  <td className="reason">{x.error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="banner ok" style={{ borderTop: "1px solid var(--border)" }}>
        Every quote is normalized and passed through the data-quality gate. Only <b>READY</b>
        {" "}(realtime · two-sided · valid · fresh) instruments enter the autonomous pipeline. The AI
        never touches IBKR directly and never sees delayed, stale, invalid or fabricated prices.
        Instruments auto-transition SUBSCRIPTION_REQUIRED → READY when their subscription becomes
        active — no code change.
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- autonomous paper trading */
export function Autonomous({ s }: { s: Snapshot | null }) {
  const a = s?.autonomous ?? null;
  const status = a?.status ?? "DISABLED";
  const statusCls = status === "RUNNING" ? "data_available" : status === "ARMED" ? "delayed"
    : status === "HALTED" || status === "KILLED" ? "halted" : "disabled";
  const cell = (label: string, value: string, cls = "") => (
    <div className="h"><span className="n">{label}</span><span className={`mono ${cls}`}>{value}</span></div>
  );
  return (
    <Section title="Autonomous trading" right={
      <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span className="pill p-delayed">PAPER</span><span className={`pill p-${statusCls}`}>{status}</span>
      </span>}>
      {!a ? <Empty label={s ? "PAPER AUTONOMOUS · DISABLED (not armed)" : NO_DATA} /> : (
        <>
          {a.dry_run ? (
            <div className="banner">● PAPER DRY RUN · NO ORDERS — full pipeline on real data, decisions logged, nothing executed.</div>
          ) : null}
          <div className="health">
            {cell("Mode", a.mode)}
            {cell("State", a.status)}
            {cell("Engine", a.engine ?? "—", a.engine === "ERROR" ? "neg" : "")}
            {cell("Data", a.data ?? "—", a.data === "REALTIME" ? "pos" : "warn")}
            {cell("Risk", a.risk ?? "—", a.risk === "ACTIVE" ? "pos" : "neg")}
            {cell("Live execution", a.live_execution ? "ON" : "DISABLED", a.live_execution ? "neg" : "")}
            {cell("Paper equity", money(a.paper_equity, 0))}
            {cell("Today's P&L", money(a.today_pnl, 0), sign(a.today_pnl))}
            {cell("Trades today", num(a.trades_today, 0))}
            {cell("Remaining daily loss", money(a.remaining_daily_loss, 0))}
            {cell("IBKR orders", num(a.ibkr_orders, 0), a.ibkr_orders ? "neg" : "")}
          </div>
          {a.metrics ? (
            <div className="health" style={{ borderTop: "1px solid var(--border)" }}>
              {cell("Evaluations", num(a.metrics.total_evaluations, 0))}
              {cell("Opportunities", num(a.metrics.opportunities_detected, 0))}
              {cell("Approved", num(a.metrics.approved_decisions, 0))}
              {cell("Rejected (risk veto)", num(a.metrics.rejected_decisions, 0))}
              {cell("NO_DATA", num(a.metrics.no_data_decisions, 0))}
              {cell("Avg confidence", a.metrics.avg_confidence == null ? "—" : pct(a.metrics.avg_confidence))}
              {cell("Avg expected risk", money(a.metrics.avg_expected_risk))}
              {cell("Avg suggested pos", money(a.metrics.avg_suggested_position, 0))}
            </div>
          ) : null}
          <div className="wrap">
            <table>
              <thead><tr><th>Time</th><th>Instrument</th><th>Source</th><th>Data</th><th>Regime</th>
                <th>Consensus</th><th>Dir</th><th>Entry</th><th>Qty</th><th>Notional</th>
                <th>Stop dist</th><th>Monetary risk</th><th>Risk % cap</th>
                <th>Risk Engine</th><th>Final decision</th><th>Reason</th></tr></thead>
              <tbody>
                {(a.decisions ?? []).length === 0
                  ? <tr><td className="empty" colSpan={16}>No decisions yet</td></tr>
                  : a.decisions.map((d, i) => (
                    <tr key={i}>
                      <td>{hhmmss(d.ts)}</td><td>{d.instrument}</td>
                      <td>{d.source ?? "—"}</td>
                      <td>{d.data_status ? <Pill text={d.data_status} /> : "—"}</td>
                      <td>{d.regime ?? "—"}</td>
                      <td>{d.consensus ?? "—"}</td>
                      <td>{d.action ?? "—"}</td>
                      <td>{price(d.entry)}</td>
                      <td>{d.suggested_size == null ? "—" : num(d.suggested_size, 0)}</td>
                      <td>{d.position_notional == null ? "—" : money(d.position_notional, 0)}</td>
                      <td>{d.stop_distance == null ? "—" : money(d.stop_distance)}</td>
                      <td>{d.monetary_risk == null ? "—" : money(d.monetary_risk, 0)}</td>
                      <td>{d.risk_pct_capital == null ? "—" : pct(d.risk_pct_capital)}</td>
                      <td>{d.risk_decision ? <Pill text={d.risk_decision} /> : "—"}</td>
                      <td><Pill text={d.final_decision ?? d.execution_decision ?? "—"} /></td>
                      <td className="reason">{d.reason}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- settings (read-only) */
export function Settings({ s }: { s: Snapshot | null }) {
  const r: any = s?.risk ?? {};
  const row = (label: string, value: string) => (
    <div className="h"><span className="n">{label}</span><span className="mono">{value}</span></div>
  );
  const mode = (s?.mode ?? "paper").toUpperCase();
  return (
    <Section title="Settings · governed server-side (read-only)"
      right={<Pill text={mode === "LIVE" ? "halted" : "delayed"} />}>
      {!s ? <Empty /> : (
        <>
          <div className="health">
            {row("Trading mode", mode)}
            {row("Execution", s.execution_enabled ? "ENABLED" : "DISABLED")}
            {row("Daily loss limit", pct(r.max_daily_loss_pct))}
            {row("Max drawdown", pct(r.max_drawdown_pct))}
            {row("Max position", pct(r.max_position_pct))}
            {row("Max leverage", isPresent(r.max_gross_leverage) ? num(r.max_gross_leverage) + "×" : NO_DATA)}
            {row("Max correlated exposure", pct(r.max_correlated_exposure_pct))}
            {row("Orders (session)", num(s.orders, 0))}
          </div>
          <div className="banner ok" style={{ borderTop: "1px solid var(--border)" }}>
            These parameters are read-only here. Trading mode, capital mandate, limits and model/
            strategy selection are changed only through the server-side governance/Risk Engine —
            never from the browser. LIVE mode requires the existing server governance, not a UI click.
          </div>
        </>
      )}
    </Section>
  );
}
