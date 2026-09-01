// Page views. Each is pure and takes `s: Snapshot | null` (+ connected) so it renders in tests via
// renderToStaticMarkup. Absent values render NO DATA — never fabricated prices, P&L, candles, positions.
import React from "react";
import type { Snapshot } from "@/lib/types";
import { NO_DATA, isPresent, money, num, pct, price, spread as fmtSpread } from "@/lib/format";
import { humanStatus } from "@/lib/errors";
import { dailyRiskUsed, engineState, riskHealth } from "@/lib/select";
import {
  Dot, Tag, NoData, ErrorDetail, Sparkline, Meter, GaugeArc, PositionCard, DecisionCard, DecisionTimeline,
} from "./ui";

function n(obj: Record<string, any> | undefined | null, key: string): number | null {
  const v = obj?.[key];
  return isPresent(v) ? v : null;
}
function Kpi({ label, big, tone, sub, children }: { label: string; big: React.ReactNode; tone?: string; sub?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="card kpi">
      <div className="label">{label}</div>
      <div className={`big num ${tone || ""}`}>{big}</div>
      {sub ? <div className="sub">{sub}</div> : null}
      {children}
    </div>
  );
}
function Disconnected() {
  return <div className="banner"><Dot tone="r" />Live backend not reachable — showing <b>&nbsp;NO DATA</b>. No values are fabricated.</div>;
}

// ---------------------------------------------------------------- OVERVIEW
export function OverviewView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const acct = s?.account || null;
  const auto = s?.autonomous || null;
  const tr = s?.trading_risk || null;
  const equity = n(acct, "equity") ?? (isPresent(auto?.paper_equity) ? auto!.paper_equity : null);
  const pnl = isPresent(auto?.today_pnl) ? auto!.today_pnl : (tr ? tr.current_daily_pnl : null);
  const used = dailyRiskUsed(s);
  const dd = n(s?.risk as any, "drawdown");
  const eng = engineState(s);
  const last = (auto?.decisions || [])[0];
  const positions = s?.positions || [];
  // Market regime is READ from the AI regime classifier's decision field — never computed in the UI.
  const regimeRaw = (auto?.decisions || []).map((d: any) => d.regime).find(Boolean) as string | undefined;
  const regimeLabel = regimeRaw ? regimeRaw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) : null;
  const regimeTone = regimeRaw ? (/up|bull|trend_up/i.test(regimeRaw) ? "g" : /down|bear/i.test(regimeRaw) ? "r" : "t") : "grey";
  return (
    <>
      {!connected ? <Disconnected /> : null}
      <div className="grid k4">
        <Kpi label="Portfolio Equity" big={money(equity, 0)} sub={<span className="neut">{s?.mode ? `${s.mode.toUpperCase()} account` : NO_DATA}</span>} />
        <Kpi label="Today's P&L" big={isPresent(pnl) ? (pnl >= 0 ? "+" : "−") + money(Math.abs(pnl), 0).slice(1) : NO_DATA} tone={isPresent(pnl) ? (pnl >= 0 ? "up" : "down") : ""} />
        <Kpi label="Daily Risk Used" big={used === null ? NO_DATA : pct(used)}>
          <div style={{ marginTop: 18 }}><Meter frac={used} color="linear-gradient(90deg,var(--accent-deep),var(--accent))" caption={used === null ? undefined : "of daily budget"} /></div>
        </Kpi>
        <Kpi label="Drawdown" big={dd === null ? NO_DATA : pct(dd)}>
          <div style={{ marginTop: 18 }}><Meter frac={dd === null ? null : Math.min(1, dd / 0.15)} color="var(--warn)" caption={dd === null ? undefined : "of 15% limit"} /></div>
        </Kpi>
      </div>

      <div className="card" style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <Dot tone={regimeTone as any} />
        <div><div className="label">Market Regime</div><div style={{ fontSize: 20, fontWeight: 650, marginTop: 4 }}>{regimeLabel || NO_DATA}</div></div>
        <div style={{ marginLeft: "auto" }} className="metrics"><span>Read from the AI regime classifier · never calculated in the UI</span></div>
      </div>

      <div className="grid k2">
        <div className="card">
          <h3>AI Trading Engine</h3>
          <div className="engine" style={{ marginTop: 20 }}>
            <div className="ring">
              <svg viewBox="0 0 112 112" aria-hidden="true"><circle cx="56" cy="56" r="48" fill="none" stroke="var(--line)" strokeWidth="6" />
                <circle cx="56" cy="56" r="48" fill="none" stroke="var(--accent)" strokeWidth="6" strokeLinecap="round" strokeDasharray="301" strokeDashoffset={eng === "RUNNING" ? 40 : eng === "ARMED" ? 150 : 260} transform="rotate(-90 56 56)" opacity=".85" /></svg>
            </div>
            <div>
              <div className="estate"><Dot tone={eng === "RUNNING" ? "g" : eng === "HALTED" || eng === "KILLED" ? "r" : eng === "ARMED" ? "t" : "grey"} />{eng}</div>
              <div className="emeta">{eng === "DISABLED" ? "Autonomous disabled — no execution" : eng === "ARMED" ? "Armed for paper session — awaiting operator START" : eng === "RUNNING" ? "Monitoring market for opportunities" : eng === "NO DATA" ? "Engine state unavailable" : "—"}</div>
              <div className="edec">
                <div className="cell"><div className="label">Opportunities</div><div className="v num">{num(auto?.metrics?.opportunities_detected as any, 0)}</div></div>
                <div className="cell"><div className="label">Last Decision</div><div className="v">{last ? <>{last.instrument} <Tag kind={(last.action || "").toString().toUpperCase() === "BUY" ? "buy" : "sell"}>{(last.action || "").toString().toUpperCase() || "—"}</Tag></> : NO_DATA}</div></div>
                <div className="cell"><div className="label">Execution</div><div className="v"><Tag kind="muted">DISABLED</Tag></div></div>
              </div>
            </div>
          </div>
        </div>
        <div className="card pad0">
          <div className="cardhead"><h3 style={{ border: 0, padding: 0 }}>Open Positions</h3><a className="link" href="/portfolio">View all →</a></div>
          {positions.length === 0 ? <div style={{ padding: 18 }}><NoData note="No open positions." /></div> : (
            <table className="rowtab"><thead><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th></tr></thead>
              <tbody>{positions.map((p, i) => {
                const q = p.quantity ?? p.qty, pn = p.unrealized_pnl ?? p.pnl;
                const side = isPresent(q) && q !== 0 ? (q > 0 ? "long" : "short") : null;
                return <tr key={i}><td>{p.symbol || p.instrument || "—"}</td>
                  <td style={{ textAlign: "right" }}>{side ? <Tag kind={side}>{side.toUpperCase()}</Tag> : NO_DATA}</td>
                  <td className="num">{num(q, 0)}</td><td className="num">{price(p.avg_price ?? p.entry)}</td>
                  <td className="num">{price(p.market_price ?? p.current ?? p.mark)}</td>
                  <td className={`num ${isPresent(pn) ? (pn >= 0 ? "up" : "down") : "neut"}`}>{isPresent(pn) ? (pn >= 0 ? "+" : "−") + money(Math.abs(pn)).slice(1) : NO_DATA}</td></tr>;
              })}</tbody></table>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- MARKETS
interface Inst { symbol: string; region?: string; source?: string | null; realtime?: boolean; bid: number | null; ask: number | null; last: number | null; volume?: number | null; latency?: number | null; status: string; code?: number | null; reason?: string | null; }
function instruments(s: Snapshot | null): Inst[] {
  if (s?.global_market_data?.length) return s.global_market_data.map((r) => ({
    symbol: r.symbol, region: r.region, source: r.source, realtime: r.realtime, bid: r.bid, ask: r.ask, last: r.last,
    volume: r.volume, latency: r.latency_ms ?? null, status: r.status, code: null, reason: r.error ?? null,
  }));
  return (s?.market_data || []).map((r) => ({
    symbol: r.symbol, region: "USA", source: null, realtime: r.market_data_type === "REALTIME",
    bid: r.bid ?? null, ask: r.ask ?? null, last: r.last ?? null, volume: null, latency: null,
    status: r.status, code: r.error_code ?? null, reason: r.reason ?? null,
  }));
}
export function MarketsView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const rows = instruments(s);
  const catalog = s?.market_catalog?.regions || {};
  const regions = ["USA", "Europe", "FX"];
  const byRegion = (rg: string) => rows.filter((r) => (r.region || "").toUpperCase().includes(rg.toUpperCase()) || (rg === "USA" && !r.region));
  const errored = rows.filter((r) => r.status === "DATA_NOT_AVAILABLE" || r.status === "ERROR");
  return (
    <>
      {!connected ? <Disconnected /> : null}
      {Object.keys(catalog).length ? <div className="card">
        <div className="cardhead"><h3 style={{ border: 0, padding: 0 }}>Global Instrument Catalog</h3><Tag kind="warnt">DISCOVERED · NOT YET TRADEABLE</Tag></div>
        <div className="grid k3">
          {Object.entries(catalog).map(([region, info]) => <div key={region}>
            <div className="label">{region}</div>
            <div className="num" style={{ fontSize: 24, marginTop: 5 }}>{num(info.discovered as any, 0)}</div>
            <div className="metrics" style={{ marginTop: 8 }}>
              <span>IBKR verified <b>{num(info.ibkr_verified as any, 0)}</b></span>
              <span>Ready <b>{num(info.ready as any, 0)}</b></span>
            </div>
          </div>)}
        </div>
      </div> : null}
      <div className="grid k3">
        {regions.map((rg) => {
          const rs = byRegion(rg);
          const live = rs.some((r) => r.status === "DATA_AVAILABLE");
          const lat = rs.map((r) => r.latency).filter(isPresent) as number[];
          const src = rs.map((r) => r.source).find(Boolean);
          return (
            <div className="card" key={rg}>
              <div className="region"><span className="rname"><Dot tone={rs.length === 0 ? "grey" : live ? "g" : "o"} />{rg}</span>
                {rs.length === 0 ? <Tag kind="muted">NO DATA</Tag> : <Tag kind={live ? "open" : "warnt"}>{live ? "OPEN" : "CLOSED"}</Tag>}</div>
              <div className="metrics" style={{ marginTop: 12 }}>
                <span>Instruments <b>{rs.length || NO_DATA}</b></span>
                <span>Source <b>{src || "—"}</b></span>
                <span>Latency <b>{lat.length ? Math.round(lat.reduce((a, b) => a + b, 0) / lat.length) + "ms" : "—"}</b></span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="card pad0">
        <div className="cardhead"><h3 style={{ border: 0, padding: 0 }}>Instruments · Data Quality</h3><span className="label">source · realtime · latency · status</span></div>
        {rows.length === 0 ? <div style={{ padding: 18 }}><NoData note="No market data. When Massive/IBKR feeds are live, instruments appear here." /></div> : (
          <table className="rowtab"><thead><tr><th>Symbol</th><th>Price</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Volume</th><th>Latency</th><th>Source</th><th>Status</th></tr></thead>
            <tbody>{rows.map((r, i) => {
              const avail = r.status === "DATA_AVAILABLE" || r.status === "DELAYED";
              return (
                // The symbol ALWAYS links to its detail terminal — the News tab (and future research)
                // works independent of market hours, so a symbol with no live quote is still navigable.
                <tr key={i} className="rowlink">
                  <td><a href={`/markets/${r.symbol}`}>{r.symbol}</a></td>
                  <td className={`num ${avail ? "" : "neut"}`}>{price(r.last)}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{price(r.bid)}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{price(r.ask)}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{fmtSpread(r.bid, r.ask)}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{isPresent(r.volume) ? num(r.volume, 0) : NO_DATA}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{isPresent(r.latency) ? Math.round(r.latency) + "ms" : NO_DATA}</td>
                  <td className={`num ${avail ? "" : "neut"}`}>{r.source || "—"}</td>
                  <td style={{ textAlign: "right" }}><Tag kind={r.status === "DATA_AVAILABLE" ? "ok" : r.status === "DELAYED" ? "warnt" : "warnt"}>{humanStatus(r.status)}</Tag></td>
                </tr>
              );
            })}</tbody></table>
        )}
        {errored.length ? <div style={{ padding: "14px 18px", borderTop: "1px solid var(--line-soft)" }}>
          {errored.map((r, i) => <div key={i} style={{ marginBottom: i < errored.length - 1 ? 8 : 0 }}>
            <ErrorDetail code={r.code} raw={`${r.symbol} · ${r.reason || "market data unavailable"}`} info /></div>)}
        </div> : null}
      </div>
    </>
  );
}


// ---------------------------------------------------------------- PORTFOLIO
export function PortfolioView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const acct = s?.account || null;
  const equity = n(acct, "equity");
  const cash = n(acct, "cash");
  const gross = n(acct, "gross_exposure");
  const net = n(acct, "net_exposure");
  const tr = s?.trading_risk || null;
  const positions = s?.positions || [];
  const grossFrac = isPresent(gross) && isPresent(equity) && equity > 0 ? gross / equity : null;
  return (
    <>
      {!connected ? <Disconnected /> : null}
      <div className="grid k4">
        <Kpi label="Equity" big={money(equity, 0)} />
        <Kpi label="Cash" big={money(cash, 0)} sub={isPresent(cash) && isPresent(equity) && equity > 0 ? <span className="neut">{pct(cash / equity)} of equity</span> : undefined} />
        <Kpi label="Gross Exposure" big={money(gross, 0)} sub={grossFrac !== null ? <span className="neut">{grossFrac.toFixed(2)}× leverage</span> : undefined} />
        <Kpi label="Daily Risk Used" big={dailyRiskUsed(s) === null ? NO_DATA : pct(dailyRiskUsed(s))} />
      </div>
      <div className="card">
        <h3 style={{ marginBottom: 14 }}>Exposure</h3>
        {grossFrac === null ? <NoData note="Exposure appears once the account has positions." /> : (
          <>
            <Meter frac={grossFrac} color="linear-gradient(90deg,var(--accent-deep),var(--accent))" caption={`gross ${pct(grossFrac)} of equity`} />
            <div className="metrics" style={{ marginTop: 14 }}>
              <span>Gross <b>{money(gross, 0)}</b></span>
              <span>Net <b>{money(net, 0)}</b></span>
              <span>Long/Short <b>{isPresent(net) && isPresent(gross) && gross > 0 ? pct((gross + net) / (2 * gross)) : NO_DATA}</b></span>
            </div>
          </>
        )}
      </div>
      <div className="card">
        <h3 style={{ marginBottom: 14 }}>Positions</h3>
        {positions.length === 0 ? <NoData note="No open positions." /> : (
          <div className="grid k2">{positions.map((p, i) => <PositionCard p={p} key={i} />)}</div>
        )}
      </div>
    </>
  );
}

// ---------------------------------------------------------------- AI BRAIN
export function AiBrainView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const auto = s?.autonomous || null;
  const m = auto?.metrics;
  const decisions = auto?.decisions || [];
  return (
    <>
      {!connected ? <Disconnected /> : null}
      <div className="grid k4">
        <Kpi label="Evaluations Today" big={num(m?.total_evaluations as any, 0)} />
        <Kpi label="Approved" big={num(m?.approved_decisions as any, 0)} tone={isPresent(m?.approved_decisions) ? "up" : ""} />
        <Kpi label="Rejected" big={num(m?.rejected_decisions as any, 0)} tone={isPresent(m?.rejected_decisions) ? "down" : ""} />
        <Kpi label="Risk Vetoes" big={num(m?.risk_vetoes as any, 0)} />
      </div>
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>Decision Timeline</h3>
        <DecisionTimeline decisions={decisions} />
      </div>
      {decisions.length === 0 ? <NoData note="No AI decisions yet. The engine is DISABLED — human ARM + START is required before any evaluation." /> :
        decisions.slice(0, 6).map((d: any, i: number) => <DecisionCard d={d} key={i} />)}
    </>
  );
}

// ---------------------------------------------------------------- RISK CENTER
export function RiskView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const tr = s?.trading_risk || null;
  const health = riskHealth(s);
  const used = dailyRiskUsed(s);
  const hClass = health === "HEALTHY" ? "healthy" : health === "WARNING" ? "warning" : health === "BLOCKED" ? "blocked" : "";
  return (
    <>
      {!connected ? <Disconnected /> : null}
      <div className={`riskhealth ${hClass}`}>
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7Z" /><path d="M9 12l2 2 4-4" /></svg>
        <div><div className="label">Risk Health</div><div className="hi">{health}</div></div>
        <div style={{ marginLeft: "auto" }} className="metrics">
          <span>Risk Used <b>{used === null ? NO_DATA : pct(used)}</b></span>
          <span>Remaining Daily Loss <b>{tr ? money(tr.remaining_daily_risk, 0) : NO_DATA}</b></span>
        </div>
      </div>
      <div className="grid k2">
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Your Parameters</h3>
          {tr ? (
            <div className="riskinputs">
              <div className="rinput"><div><div className="label">Trading Capital</div><div className="hint">capital mandate</div></div><div className="v num">{money(tr.capital, 0)}</div></div>
              <div className="rinput"><div><div className="label">Risk per Trade</div><div className="hint">max loss on a single trade</div></div><div className="v num">{pct(tr.risk_per_trade_pct)}</div></div>
              <div className="rinput"><div><div className="label">Max Daily Loss</div><div className="hint">max loss in one trading day</div></div><div className="v num">{pct(tr.max_daily_loss_pct)}</div></div>
            </div>
          ) : <NoData note="Risk parameters unavailable." />}
        </div>
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>Daily Loss Budget</h3>
          <div className="riskgauge"><GaugeArc frac={used} sub="USED" color={health === "BLOCKED" ? "var(--neg)" : health === "WARNING" ? "var(--warn)" : "var(--accent)"} /></div>
          <div className="autobox" style={{ marginTop: 10 }}>
            <div className="autocell"><div className="label">Max Risk / Trade</div><div className="v num">{tr ? money(tr.max_risk_per_trade, 0) : NO_DATA}</div></div>
            <div className="autocell"><div className="label">Max Daily Loss</div><div className="v num">{tr ? money(tr.max_daily_loss, 0) : NO_DATA}</div></div>
            <div className="autocell"><div className="label">Remaining Today</div><div className="v num up">{tr ? money(tr.remaining_daily_risk, 0) : NO_DATA}</div></div>
          </div>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- SYSTEM
const SERVICE_LABELS: [string, string][] = [
  ["market_data", "Market Data"], ["trading_core", "Trading Core"], ["trading", "Trading Core"],
  ["risk", "Risk Engine"], ["broker", "Broker Connector"], ["database", "Database"], ["redis", "Redis Cache"],
];
export function SystemView({ s, connected }: { s: Snapshot | null; connected: boolean }) {
  const h = s?.system_health || {};
  const seen = new Set<string>();
  const rows = SERVICE_LABELS.filter(([k]) => { if (seen.has(k) || !(k in h)) return false; seen.add(k); return true; })
    .map(([k, label]) => ({ label, status: (h as any)[k] as string }));
  const displayRows = rows.length ? rows : SERVICE_LABELS.slice(0, 6).map(([, label]) => ({ label, status: "" }));
  const tone = (st: string) => /healthy|up|ok|online/i.test(st) ? "g" : /degraded|warn|stale/i.test(st) ? "o" : /down|fail|error/i.test(st) ? "r" : "grey";
  return (
    <>
      {!connected ? <Disconnected /> : null}
      <div className="card pad0">
        <div className="cardhead"><h3 style={{ border: 0, padding: 0 }}>System Health</h3>{rows.length ? <Tag kind="ok">{rows.every((r) => /healthy|up|ok|online/i.test(r.status)) ? "ALL HEALTHY" : "DEGRADED"}</Tag> : <Tag kind="muted">NO DATA</Tag>}</div>
        <div className="health">{displayRows.map((r, i) => (
          <div className="hrow" key={i}><span className="n"><Dot tone={tone(r.status) as any} />{r.label}</span>
            <span className={`hstat ${tone(r.status)}`}>{r.status ? r.status.toUpperCase() : NO_DATA}</span></div>
        ))}</div>
      </div>
      <div className="card">
        <details><summary><span className="chev">▸</span>Advanced Diagnostics</summary>
          <table className="diag"><tbody>
            <tr><td>broker · connection</td><td>{(h as any).broker ? String((h as any).broker).toUpperCase() : NO_DATA}</td></tr>
            <tr><td>runtime state</td><td>{s?.autonomous?.status || s?.system_status || NO_DATA}</td></tr>
            <tr><td>execution enabled</td><td>{s ? String(s.execution_enabled === true) : NO_DATA}</td></tr>
            <tr><td>IBKR orders</td><td>{isPresent(s?.autonomous?.ibkr_orders as any) ? String(s!.autonomous!.ibkr_orders) : NO_DATA}</td></tr>
            <tr><td>last incident</td><td>{(s?.notifications || []).find((x: any) => /error|halt|incident/i.test(x?.level || x?.type || ""))?.message || (s ? "none" : NO_DATA)}</td></tr>
            <tr><td>last restart</td><td>{(s?.autonomous?.audit || []).find((a: any) => /recover|restart|boot/i.test(a?.reason || ""))?.ts || NO_DATA}</td></tr>
            <tr><td>recovery state</td><td>{/RECOVERY/i.test(s?.system_status || "") ? s!.system_status : (s ? "nominal" : NO_DATA)}</td></tr>
            <tr><td>data generated at</td><td>{s?.generated_at || NO_DATA}</td></tr>
          </tbody></table>
        </details>
      </div>
    </>
  );
}
