"use client";
// Backtesting Research page (§ Phase R3.0). RESEARCH ONLY — a backtest is an internal historical research
// run over stored OHLC. It never trades, never places or submits an order, never enables execution. Every
// number traces to a real backtest record or is rendered NO DATA / INSUFFICIENT / NOT APPLICABLE.
import React, { useState } from "react";
import { NO_DATA } from "@/lib/format";
import {
  fetchBacktestMetrics, fetchBacktestTrades, fetchBacktestEquity, fetchBacktestEvents,
} from "@/lib/api";
import {
  type BacktestRun, type BacktestMetrics, type BacktestTrade, type EquityPoint, type BacktestEvent,
  statusTone, metricText, pctText,
} from "@/lib/backtest";
import { BacktestForm } from "./BacktestForm";

function SafetyBanner() {
  return (
    <div className="bt-banner">
      <span className="bt-b-tag">RESEARCH ONLY</span>
      <span className="bt-b-tag">NOT LIVE TRADING</span>
      <span className="bt-b-pill">AUTONOMOUS <b>DISABLED</b></span>
      <span className="bt-b-pill">EXECUTION <b>DISABLED</b></span>
      <span className="bt-b-pill">IBKR ORDERS <b>0</b></span>
    </div>
  );
}

function EquityChart({ equity }: { equity: EquityPoint[] }) {
  const pts = equity.map((e) => e.equity).filter((v): v is number => typeof v === "number");
  if (pts.length < 2) return <div className="nodata"><div className="nd">{NO_DATA}</div><p>No equity curve — the run produced fewer than two points.</p></div>;
  const W = 900, H = 240, PL = 8, PR = 54, PW = W - PL - PR, top = 10, ph = 150, dt = 176, dh = 54;
  const min = Math.min(...pts), max = Math.max(...pts), span = max - min || 1;
  const dds = equity.map((e) => (typeof e.drawdown_pct === "number" ? e.drawdown_pct : 0));
  const ddMax = Math.max(1, ...dds);
  const x = (i: number) => PL + (i / (pts.length - 1)) * PW;
  const yE = (v: number) => top + (max - v) / span * ph;
  const line = pts.map((v, i) => `${x(i).toFixed(1)},${yE(v).toFixed(1)}`).join(" ");
  const ddArea = dds.map((v, i) => `${x(i).toFixed(1)},${(dt + (v / ddMax) * dh).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="bt-chart" role="img" aria-label="Backtest equity curve and drawdown">
      {[0, 1, 2, 3, 4].map((g) => {
        const val = max - span * g / 4, y = yE(val);
        return <g key={g}><line x1={PL} y1={y} x2={PL + PW} y2={y} stroke="var(--line-soft)" strokeWidth="1" />
          <text x={PL + PW + 6} y={y + 3} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10">{Math.round(val).toLocaleString()}</text></g>;
      })}
      <text x={PL + 3} y={top + 12} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">EQUITY</text>
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="1.6" />
      <line x1={PL} y1={dt} x2={PL + PW} y2={dt} stroke="var(--line-soft)" strokeWidth="1" />
      <text x={PL + 3} y={dt + 12} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">DRAWDOWN %</text>
      <polyline points={`${PL},${dt} ${ddArea} ${PL + PW},${dt}`} fill="var(--neg-bg)" stroke="var(--neg)" strokeWidth="1.2" />
    </svg>
  );
}

const METRIC_ROWS: [string, string, (m: Record<string, unknown>) => string][] = [
  ["Total Return", "total_return", (m) => pctText(m.total_return)],
  ["Annualized", "annualized_return", (m) => (typeof m.annualized_return === "number" ? pctText(m.annualized_return) : metricText(m.annualized_return))],
  ["Max Drawdown", "max_drawdown", (m) => pctText(m.max_drawdown)],
  ["Sharpe", "sharpe", (m) => metricText(m.sharpe, 2)],
  ["Sortino", "sortino", (m) => metricText(m.sortino, 2)],
  ["Volatility", "volatility", (m) => (typeof m.volatility === "number" ? pctText(m.volatility) : metricText(m.volatility))],
  ["Profit Factor", "profit_factor", (m) => metricText(m.profit_factor, 2)],
  ["Win Rate", "win_rate", (m) => (typeof m.win_rate === "number" ? pctText(m.win_rate) : metricText(m.win_rate))],
  ["Expectancy", "expectancy", (m) => metricText(m.expectancy, 2)],
  ["Payoff Ratio", "payoff_ratio", (m) => metricText(m.payoff_ratio, 2)],
  ["Trades", "num_trades", (m) => metricText(m.num_trades, 0)],
  ["Exposure Time", "exposure_time", (m) => (typeof m.exposure_time === "number" ? pctText(m.exposure_time) : metricText(m.exposure_time))],
  ["Turnover", "turnover", (m) => metricText(m.turnover, 2, "×")],
  ["Total Commissions", "total_commissions", (m) => "$" + metricText(m.total_commissions, 2)],
  ["Total Slippage", "total_slippage", (m) => "$" + metricText(m.total_slippage, 2)],
  ["Best / Worst", "best_trade", (m) => `${metricText(m.best_trade, 0)} / ${metricText(m.worst_trade, 0)}`],
];

export function Backtesting({ connected }: { connected: boolean }) {
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [metrics, setMetrics] = useState<BacktestMetrics | null>(null);
  const [trades, setTrades] = useState<BacktestTrade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [events, setEvents] = useState<BacktestEvent[]>([]);

  async function onRun(r: BacktestRun) {
    setRun(r); setMetrics(null); setTrades([]); setEquity([]); setEvents([]);
    if (r.status === "COMPLETED") {
      const [m, t, e, ev] = await Promise.all([
        fetchBacktestMetrics(r.run_id).catch(() => null),
        fetchBacktestTrades(r.run_id).catch(() => []),
        fetchBacktestEquity(r.run_id).catch(() => []),
        fetchBacktestEvents(r.run_id).catch(() => []),
      ]);
      setMetrics(m); setTrades(t); setEquity(e); setEvents(ev);
    } else {
      setEvents(await fetchBacktestEvents(r.run_id).catch(() => []));
    }
  }

  const tone = statusTone(run?.status);
  const m = (metrics?.metrics as Record<string, unknown>) || null;
  const robustness = (m?.robustness as Record<string, unknown>) || null;
  const coverage = run?.missing_data?.symbols || [];

  return (
    <>
      {!connected ? <div className="banner"><span className="dot r" aria-hidden="true" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No results are fabricated.</div> : null}
      <SafetyBanner />
      <BacktestForm connected={connected} onRun={onRun} />

      {run ? (
        <>
          <div className={`bt-status ${tone}`}>
            <div><div className="label">Run</div><div className="bt-runid num">{run.run_id.slice(0, 12)}…</div></div>
            <div><div className="label">Status</div><div className={`bt-state ${tone}`}>{run.status}</div></div>
            <div><div className="label">Strategy</div><div>{run.strategy_id} v{run.strategy_version}</div></div>
            <div><div className="label">Universe</div><div>{run.symbols.join(", ")} · {run.interval}</div></div>
            <div><div className="label">Checksum</div><div className="num bt-ck">{run.result_checksum ? run.result_checksum.slice(0, 12) : NO_DATA}</div></div>
          </div>

          {run.status === "FAILED" ? (
            <div className="card bt-failed">
              <div className="bt-fail-code">{run.failure_code || "FAILED"}</div>
              <p>{run.failure_reason || "The run failed. No results were fabricated."}</p>
              {coverage.length ? (
                <table className="bt-cov"><thead><tr><th>Symbol</th><th>Expected</th><th>Available</th><th>Missing</th><th>Usable</th><th>Reason</th></tr></thead>
                  <tbody>{coverage.map((c) => <tr key={c.symbol}><td>{c.symbol}</td><td className="num">{c.expected_bars}</td><td className="num">{c.available_bars}</td>
                    <td className="num">{c.missing_bars} ({(c.missing_ratio * 100).toFixed(1)}%)</td><td className="num">{c.usable_bars}</td><td>{c.reason || "—"}</td></tr>)}</tbody></table>
              ) : null}
            </div>
          ) : run.status === "COMPLETED" ? (
            <>
              <div className="card"><h3 style={{ marginBottom: 12 }}>Equity &amp; Drawdown</h3><EquityChart equity={equity} /></div>

              <div className="card"><h3 style={{ marginBottom: 14 }}>Performance Metrics</h3>
                {m ? (
                  <div className="bt-metrics">{METRIC_ROWS.map(([label, key, fn]) => (
                    <div className="bt-m" key={key}><div className="label">{label}</div><div className="v num">{fn(m)}</div></div>))}</div>
                ) : <div className="nodata"><div className="nd">{NO_DATA}</div><p>Metrics unavailable.</p></div>}
              </div>

              <div className="grid k2">
                <div className="card"><h3 style={{ marginBottom: 8 }}>Trade Ledger</h3>
                  {trades.length ? (
                    <div className="bt-ledger-wrap"><table className="bt-ledger"><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Net P&amp;L</th><th>Exit</th></tr></thead>
                      <tbody>{trades.map((t) => <tr key={t.id}><td>{t.symbol}</td><td className="num">{t.entry_price?.toFixed(2) ?? NO_DATA}</td>
                        <td className="num">{t.exit_price?.toFixed(2) ?? NO_DATA}</td><td className="num">{t.quantity ?? NO_DATA}</td>
                        <td className={`num ${(t.net_pnl ?? 0) >= 0 ? "up" : "down"}`}>{t.net_pnl == null ? NO_DATA : (t.net_pnl >= 0 ? "+" : "") + t.net_pnl.toFixed(2)}</td>
                        <td><span className="bt-reason">{t.exit_reason}{t.ambiguous ? " ⚠" : ""}</span></td></tr>)}</tbody></table></div>
                  ) : <div className="nodata"><div className="nd">{NO_DATA}</div><p>No trades in this run.</p></div>}
                </div>

                <div className="card"><h3 style={{ marginBottom: 8 }}>Robustness &amp; Coverage</h3>
                  {robustness ? (
                    <div className="bt-rob">
                      <div className="bt-r"><span className="label">Missing-data ratio</span><b className="num">{metricText(robustness.missing_data_ratio, 3)}</b></div>
                      <div className="bt-r"><span className="label">In-sample return</span><b className="num">{typeof robustness.in_sample_return === "number" ? pctText(robustness.in_sample_return) : metricText(robustness.in_sample_return)}</b></div>
                      <div className="bt-r"><span className="label">Out-of-sample return</span><b className="num">{typeof robustness.out_of_sample_return === "number" ? pctText(robustness.out_of_sample_return) : metricText(robustness.out_of_sample_return)}</b></div>
                      <div className="bt-r"><span className="label">Symbol concentration</span><b className="num">{metricText(robustness.concentration_by_symbol, 3)}</b></div>
                      {robustness.min_trade_warning ? <div className="bt-warn">⚠ {String(robustness.min_trade_warning)}</div> : null}
                      <p className="bt-disclaimer">{String(robustness.disclaimer || "")}</p>
                    </div>
                  ) : <div className="nodata"><div className="nd">{NO_DATA}</div></div>}
                </div>
              </div>
            </>
          ) : null}

          {events.length ? (
            <div className="card"><h3 style={{ marginBottom: 8 }}>Run Events</h3>
              <div className="bt-events">{events.map((e) => (
                <div className={`bt-ev ${e.severity === "CRITICAL" ? "blocked" : e.severity === "WARNING" ? "warning" : ""}`} key={e.id}>
                  <span className="bt-ev-type">{e.event_type}</span>{e.symbol ? <span className="bt-ev-sym">{e.symbol}</span> : null}
                  <span className="bt-ev-det num">{Object.keys(e.details || {}).length ? JSON.stringify(e.details).slice(0, 90) : ""}</span></div>))}</div>
            </div>
          ) : null}

          <div className="card"><details><summary><span className="chev">▸</span>Configuration &amp; Audit</summary>
            <div className="bt-audit">
              <div className="bt-a"><span className="label">Engine</span><b className="num">{run.engine_version ?? NO_DATA}</b></div>
              <div className="bt-a"><span className="label">Timestamp policy</span><b>{run.timestamp_policy_id ?? NO_DATA}</b></div>
              <div className="bt-a"><span className="label">Exchange TZ</span><b>{run.exchange_tz ?? NO_DATA}</b></div>
              <div className="bt-a"><span className="label">Asset class</span><b>{run.asset_class ?? NO_DATA}</b></div>
              <div className="bt-a"><span className="label">Result checksum</span><b className="num bt-ck">{run.result_checksum ?? NO_DATA}</b></div>
              <div className="bt-a"><span className="label">Created</span><b className="num">{run.created_at ?? NO_DATA}</b></div>
            </div>
            {run.warnings?.length ? <ul className="bt-warns">{run.warnings.map((w) => <li key={w}>⚠ {w}</li>)}</ul> : null}
          </details></div>
        </>
      ) : (
        <div className="card"><div className="nodata"><div className="nd">NO RUN YET</div>
          <p>Configure a strategy and click <b>Run Backtest</b> to start an internal historical research run. This never trades and never places an order.</p></div></div>
      )}
    </>
  );
}
