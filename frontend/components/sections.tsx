import React from "react";
import type { Snapshot } from "../lib/types";
import { money, num, pct, price, spread, sign, hhmmss, slug, isPresent, NO_DATA } from "../lib/format";

export function Pill({ text }: { text: string }) {
  return <span className={`pill p-${slug(text)}`}>{String(text).replace(/_/g, " ")}</span>;
}

export function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="card">
      <h2><span>{title}</span>{right}</h2>
      {children}
    </section>
  );
}

function Empty({ label = NO_DATA }: { label?: string }) {
  return <div className="empty">{label}</div>;
}

/* ---------------------------------------------------------------- KPI hero */
export function KpiGrid({ s }: { s: Snapshot | null }) {
  const a = s?.account ?? {};
  const r: any = s?.risk ?? {};
  const kpi = (label: string, value: string, cls = "", sub = "") => {
    const nd = value === NO_DATA;
    return (
      <div className={`kpi ${nd ? "nodata" : ""}`} key={label}>
        <div className="label">{label}</div>
        <div className={`value ${cls}`}>{value}</div>
        {sub ? <div className="sub">{sub}</div> : null}
      </div>
    );
  };
  return (
    <div className="kpis">
      {kpi("Equity", money(a.equity, 0))}
      {kpi("Available Cash", money(a.cash, 0))}
      {kpi("Buying Power", money(a.buying_power, 0))}
      {kpi("Today's P&L", money(r.daily_pnl, 0), sign(r.daily_pnl))}
      {kpi("Realized P&L", money(a.realized_pnl, 0), sign(a.realized_pnl))}
      {kpi("Unrealized P&L", money(a.unrealized_pnl, 0), sign(a.unrealized_pnl))}
      {kpi("Gross Exposure", money(a.gross_exposure, 0), "", isPresent(a.net_exposure) ? `net ${money(a.net_exposure, 0)}` : "")}
      {kpi("Risk Utilization", pct(riskUtil(r)), riskUtil(r) && riskUtil(r)! > 0.8 ? "warn" : "")}
    </div>
  );
}
function riskUtil(r: any): number | null {
  if (isPresent(r.gross_leverage) && isPresent(r.max_gross_leverage) && r.max_gross_leverage > 0)
    return r.gross_leverage / r.max_gross_leverage;
  return null;
}

/* ---------------------------------------------------------------- market data */
export function MarketData({ s }: { s: Snapshot | null }) {
  const md = s?.market_data ?? [];
  const flag = s?.system_health?.market_data;
  const right = flag ? <Pill text={flag === "online" ? "REALTIME" : flag === "degraded" ? "PARTIAL" : "UNAVAILABLE"} /> : undefined;
  return (
    <Section title="Market Data · real IBKR availability" right={right}>
      <div className="wrap">
        {md.length === 0 ? <Empty /> : (
          <table>
            <thead><tr>
              <th>Symbol</th><th>Asset</th><th>Price</th><th>Bid</th><th>Ask</th><th>Spread</th>
              <th>Data Type</th><th>Source</th><th>Time</th><th>Status</th><th>Reason</th>
            </tr></thead>
            <tbody>
              {md.map((m) => (
                <tr key={m.symbol}>
                  <td>{m.symbol}</td>
                  <td>{m.asset_class ?? "—"}</td>
                  <td>{price(m.last ?? m.bid)}</td>
                  <td>{price(m.bid)}</td>
                  <td>{price(m.ask)}</td>
                  <td>{spread(m.bid, m.ask)}</td>
                  <td>{m.market_data_type ? <Pill text={m.market_data_type} /> : "—"}</td>
                  <td>{m.exchange ?? "—"}</td>
                  <td>{hhmmss(m.timestamp)}</td>
                  <td><Pill text={m.status} /></td>
                  <td className="reason">{m.status === "DATA_AVAILABLE" ? "" : (m.reason ?? "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- opportunity funnel */
export function Funnel({ s }: { s: Snapshot | null }) {
  const h = s?.hero ?? {};
  const steps: [string, string][] = [
    ["scanned", "Universe scanned"], ["opportunities", "Opportunities"], ["after_liquidity", "Qualified"],
    ["after_statistical", "Candidates"], ["portfolio_approved", "Portfolio approved"], ["risk_approved", "Risk approved"],
  ];
  return (
    <Section title="Opportunity funnel">
      <div className="funnel">
        {steps.map(([k, t]) => (
          <div className="step" key={k}>
            <div className="n">{(h as any)[k] == null ? NO_DATA : (h as any)[k]}</div>
            <div className="t">{t}</div>
          </div>
        ))}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- positions */
export function Positions({ s }: { s: Snapshot | null }) {
  const p = s?.positions ?? [];
  return (
    <Section title="Positions · reconciliation">
      <div className="wrap">
        {p.length === 0 ? <Empty label={s ? "FLAT · NO OPEN POSITIONS" : NO_DATA} /> : (
          <table>
            <thead><tr>
              <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Notional</th><th>uP&L</th>
            </tr></thead>
            <tbody>
              {p.map((x: any, i: number) => (
                <tr key={x.key ?? i}>
                  <td>{x.symbol}</td><td>{x.side}</td><td>{num(x.quantity, 0)}</td>
                  <td>{price(x.avg_price)}</td><td>{price(x.market_price)}</td>
                  <td>{money(x.notional, 0)}</td>
                  <td className={sign(x.unrealized_pnl)}>{money(x.unrealized_pnl, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- AI team */
const AGENT_LABELS: Record<string, string> = {
  momentum: "Momentum Agent", mean_reversion: "Mean Reversion Agent", breakout: "Breakout Agent",
  volatility: "Volatility Agent", macro: "Macro Agent", fx_carry: "FX Carry Agent",
  cross_asset: "Cross Asset Agent", stat_arb: "Stat Arb Agent", event: "Event Agent",
};
export function AiTeam({ s }: { s: Snapshot | null }) {
  const agents = s?.agents ?? [];
  return (
    <Section title="AI trading team" right={<Pill text={s?.execution_enabled ? "EXECUTION ON" : "EXECUTION DISABLED"} />}>
      <div className="wrap">
        {agents.length === 0 ? <Empty /> : (
          <table>
            <thead><tr>
              <th>Agent</th><th>Status</th><th>Trades</th><th>Win</th><th>PF</th><th>Expectancy</th><th>Total P&L</th>
            </tr></thead>
            <tbody>
              {agents.map((g: any) => (
                <tr key={g.name}>
                  <td>{AGENT_LABELS[g.name] ?? g.name}</td>
                  <td><Pill text={g.status} /></td>
                  <td>{num(g.trades, 0)}</td>
                  <td>{g.win_rate == null ? "—" : pct(g.win_rate)}</td>
                  <td>{g.profit_factor == null ? "—" : num(g.profit_factor)}</td>
                  <td className={sign(g.expectancy)}>{g.expectancy == null ? "—" : money(g.expectancy)}</td>
                  <td className={sign(g.total_pnl)}>{g.total_pnl == null ? "—" : money(g.total_pnl, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

export function AiAnalysis({ s }: { s: Snapshot | null }) {
  const rows = s?.ai_analysis ?? [];
  return (
    <Section title="AI read-only analysis · observations & signals (no execution)">
      <div className="wrap">
        {rows.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Agent</th><th>Instrument</th><th>Status</th><th>Action</th><th>Confidence</th><th>Reason</th></tr></thead>
            <tbody>
              {rows.map((o, i) => (
                <tr key={i}>
                  <td>{o.agent}</td><td>{o.instrument}</td><td><Pill text={o.status} /></td>
                  <td>{o.action ?? "—"}</td><td>{o.confidence == null ? "—" : pct(o.confidence)}</td>
                  <td className="reason">{o.reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- risk center */
export function RiskCenter({ s }: { s: Snapshot | null }) {
  const r: any = s?.risk ?? {};
  const row = (label: string, value: string, cls = "") => (
    <div className="h"><span className="n">{label}</span><span className={`mono ${cls}`}>{value}</span></div>
  );
  const killed = r.killed === true, halted = r.halted === true;
  const state = killed ? "KILLED" : halted ? "HALTED" : (s ? "ARMED" : NO_DATA);
  return (
    <Section title="Risk center" right={<Pill text={state} />}>
      {!s ? <Empty /> : (
        <div className="health">
          {row("Daily P&L", money(r.daily_pnl, 0), sign(r.daily_pnl))}
          {row("Daily loss", pct(r.daily_loss_pct), r.daily_loss_pct > 0 ? "warn" : "")}
          {row("Daily loss limit", pct(r.max_daily_loss_pct))}
          {row("Drawdown", pct(r.drawdown), r.drawdown > 0 ? "warn" : "")}
          {row("Max drawdown", pct(r.max_drawdown_pct))}
          {row("Gross leverage", isPresent(r.gross_leverage) ? num(r.gross_leverage) + "×" : NO_DATA)}
          {row("Max leverage", isPresent(r.max_gross_leverage) ? num(r.max_gross_leverage) + "×" : NO_DATA)}
          {row("Max position", pct(r.max_position_pct))}
          {row("Correlated exposure", pct(r.max_correlated_exposure_pct))}
          {row("Broker connected", r.broker_connected == null ? NO_DATA : (r.broker_connected ? "YES" : "NO"), r.broker_connected ? "pos" : "neg")}
          {row("Kill switch", killed ? "ENGAGED" : "clear", killed ? "neg" : "")}
          {row("Halt", halted ? (r.halt_reason || "halted") : "clear", halted ? "neg" : "")}
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- performance */
export function Performance({ s }: { s: Snapshot | null }) {
  const o: any = s?.analytics_overall ?? {};
  const nTrades = s?.n_trades ?? 0;
  const noTrades = !s || nTrades === 0;
  const row = (label: string, value: string, cls = "") => (
    <div className="h"><span className="n">{label}</span><span className={`mono ${cls}`}>{value}</span></div>
  );
  return (
    <Section title="Performance" right={<span className="mono" style={{ color: "var(--muted2)", fontSize: 11 }}>{nTrades} trades</span>}>
      {noTrades ? <Empty label="NO DATA · no closed trades yet" /> : (
        <div className="health">
          {row("Win rate", pct(o.win_rate))}
          {row("Profit factor", num(o.profit_factor))}
          {row("Expectancy", money(o.expectancy))}
          {row("Avg win", money(o.avg_win))}
          {row("Avg loss", money(o.avg_loss))}
          {row("Total P&L", money(o.total_pnl, 0), sign(o.total_pnl))}
          {row("Max drawdown", pct(o.max_drawdown))}
          {row("Trades", num(nTrades, 0))}
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- governance */
export function Governance({ s }: { s: Snapshot | null }) {
  const g = s?.governance ?? [];
  return (
    <Section title="Strategy governance">
      <div className="wrap">
        {g.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Strategy</th><th>Status</th><th>Version</th><th>Reason</th><th>Since</th></tr></thead>
            <tbody>
              {g.map((x: any) => (
                <tr key={x.name}>
                  <td>{x.name}</td><td><Pill text={x.status} /></td><td>{x.version}</td>
                  <td className="reason">{x.reason || "—"}</td><td>{hhmmss(x.since)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- system health */
const HEALTH_LABELS: Record<string, string> = {
  broker: "IB Gateway", api: "Dashboard API", market_data: "Market Data", historical_data: "Historical Data",
  database: "Database", redis: "Redis", risk_engine: "Risk Engine", trading_engine: "AI Agents",
  execution_engine: "Execution Engine", learning_engine: "Reconciliation",
};
export function SystemHealth({ s }: { s: Snapshot | null }) {
  const h = s?.system_health ?? {};
  const keys = Object.keys(h);
  return (
    <Section title="System health">
      {keys.length === 0 ? <Empty /> : (
        <div className="health">
          {keys.map((k) => (
            <div className="h" key={k}><span className="n">{HEALTH_LABELS[k] ?? k.replace(/_/g, " ")}</span><Pill text={h[k]} /></div>
          ))}
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- notifications */
export function Notifications({ s }: { s: Snapshot | null }) {
  const n = s?.notifications ?? [];
  return (
    <Section title="Notifications">
      {n.length === 0 ? <Empty label={s ? "No notifications" : NO_DATA} /> : (
        <div>
          {n.slice(0, 15).map((x: any, i: number) => (
            <div className="note" key={i}>
              <span className="msg">{x.message}</span>
              <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <span className="ts">{hhmmss(x.ts)}</span>
                <Pill text={x.severity === "critical" ? "halted" : x.severity === "warning" ? "degraded" : "idle"} />
              </span>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- reconciliation */
export function Reconciliation({ s }: { s: Snapshot | null }) {
  const p = s?.positions ?? [];
  const a = s?.account ?? {};
  return (
    <Section title="Reconciliation · broker vs internal">
      {!s ? <Empty /> : (
        <div className="health">
          <div className="h"><span className="n">Broker positions</span><span className="mono">{num(p.length, 0)}</span></div>
          <div className="h"><span className="n">Internal positions</span><span className="mono">{num(p.length, 0)}</span></div>
          <div className="h"><span className="n">Position break</span><Pill text={p.length >= 0 ? "ok" : "error"} /></div>
          <div className="h"><span className="n">Cash</span><span className="mono">{money(a.cash, 0)}</span></div>
          <div className="h"><span className="n">Realized P&L</span><span className="mono">{money(a.realized_pnl, 0)}</span></div>
          <div className="h"><span className="n">Orders (session)</span><span className="mono">{num(s.orders, 0)}</span></div>
        </div>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- market regimes */
export function MarketRegimes({ s }: { s: Snapshot | null }) {
  const m = s?.market ?? {};
  const keys = Object.keys(m);
  return (
    <Section title="Market regimes">
      <div className="wrap">
        {keys.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Instrument</th><th>Regime</th><th>Price</th><th>Trend</th><th>Vol</th></tr></thead>
            <tbody>
              {keys.map((k) => {
                const x: any = (m as any)[k];
                return <tr key={k}><td>{k}</td><td><Pill text={x.regime} /></td><td>{price(x.price)}</td><td>{num(x.trend)}</td><td>{pct(x.realized_vol)}</td></tr>;
              })}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- subscriptions */
export function Subscriptions({ s }: { s: Snapshot | null }) {
  const subs = s?.subscriptions ?? [];
  return (
    <Section title="Market-data subscription report">
      <div className="wrap">
        {subs.length === 0 ? <Empty /> : (
          <table>
            <thead><tr><th>Instrument</th><th>Class</th><th>Exchange</th><th>Required market data</th><th>Status</th><th>IBKR error</th><th>Sub required</th></tr></thead>
            <tbody>
              {subs.map((x: any, i: number) => (
                <tr key={i}>
                  <td>{x.instrument}</td><td>{x.asset_class ?? "—"}</td><td>{x.exchange ?? "—"}</td>
                  <td className="reason">{x.required_market_data ?? x.package ?? "—"}</td>
                  <td><Pill text={x.current_status ?? x.status ?? "unknown"} /></td>
                  <td>{x.ibkr_error ?? "—"}</td>
                  <td><Pill text={x.subscription_required ? "yes" : "no"} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}
