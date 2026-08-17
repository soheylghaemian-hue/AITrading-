"use client";
// Backtest configuration form (§ Phase R3.0). Starts an INTERNAL historical research run. The action is
// "Run Backtest" — never "Trade", "Execute", or "Place Order". It never enables trading, never creates a
// broker order, never touches execution. Authorized by the server proxy (owner token injected server-side).
import React, { useState } from "react";
import { createBacktest, type BacktestCreateReq } from "@/lib/api";
import type { BacktestRun } from "@/lib/backtest";

type Fields = {
  symbols: string; interval: string; start: string; end: string; starting_capital: string; currency: string;
  spread_bps: string; slippage_bps: string; commission_per_share: string; min_commission: string;
  risk_per_trade_pct: string; max_daily_loss_pct: string; max_portfolio_exposure_pct: string;
  max_drawdown_pct: string; warning_threshold_pct: string; max_concurrent_positions: string;
};

const DEFAULTS: Fields = {
  symbols: "NVDA", interval: "1D", start: "2026-01-05", end: "2026-06-30", starting_capital: "100000",
  currency: "USD", spread_bps: "2", slippage_bps: "1", commission_per_share: "0.005", min_commission: "1",
  risk_per_trade_pct: "1", max_daily_loss_pct: "2", max_portfolio_exposure_pct: "100", max_drawdown_pct: "20",
  warning_threshold_pct: "80", max_concurrent_positions: "3",
};

export function BacktestForm({ connected, onRun }: { connected: boolean; onRun: (run: BacktestRun) => void }) {
  const [f, setF] = useState<Fields>(DEFAULTS);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof Fields) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF((p) => ({ ...p, [k]: e.target.value }));

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrors([]);
    const symbols = f.symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (symbols.length === 0) { setErrors(["at least one symbol is required"]); return; }
    const req: BacktestCreateReq = {
      strategy_id: "OHLC_TREND_BASELINE", strategy_version: 1, symbols, interval: f.interval,
      start: f.start, end: f.end, starting_capital: f.starting_capital, currency: f.currency,
      max_concurrent_positions: Number(f.max_concurrent_positions),
      costs: { spread_bps: f.spread_bps, slippage_bps: f.slippage_bps,
        commission_per_share: f.commission_per_share, min_commission: f.min_commission },
      risk: { risk_per_trade_pct: f.risk_per_trade_pct, max_daily_loss_pct: f.max_daily_loss_pct,
        max_portfolio_exposure_pct: f.max_portfolio_exposure_pct, max_drawdown_pct: f.max_drawdown_pct,
        warning_threshold_pct: f.warning_threshold_pct },
    };
    setBusy(true);
    try {
      const r = await createBacktest(req);
      if (r.ok && r.data) onRun(r.data);
      else if (r.errors?.length) setErrors(r.errors);
      else setErrors([r.detail || "run failed to start"]);
    } finally { setBusy(false); }
  }

  const F = ({ k, label, hint }: { k: keyof Fields; label: string; hint?: string }) => (
    <label className="bt-f"><span className="label">{label}</span>
      <input className="num" value={f[k]} onChange={set(k)} inputMode="decimal" />
      {hint ? <span className="bt-hint">{hint}</span> : null}</label>
  );

  return (
    <form className="card btform" onSubmit={onSubmit}>
      <div className="bt-strat">
        <span className="label">Strategy</span>
        <b>OHLC_TREND_BASELINE v1</b>
        <span className="bt-hint">SMA 20/50 · ATR-14 stop (2×ATR) · long-only · deterministic · no optimizer</span>
      </div>
      <div className="bt-grid">
        <label className="bt-f bt-wide"><span className="label">Symbols (US equities)</span>
          <input value={f.symbols} onChange={set("symbols")} placeholder="NVDA, AAPL" /></label>
        <label className="bt-f"><span className="label">Interval</span>
          <select value={f.interval} onChange={set("interval")}><option value="1D">1D</option><option value="1h">1h</option></select></label>
        <label className="bt-f"><span className="label">Start</span><input type="date" value={f.start} onChange={set("start")} /></label>
        <label className="bt-f"><span className="label">End</span><input type="date" value={f.end} onChange={set("end")} /></label>
        <F k="starting_capital" label="Starting Capital" />
        <label className="bt-f"><span className="label">Currency</span>
          <select value={f.currency} onChange={set("currency")}>{["USD", "EUR", "GBP"].map((c) => <option key={c}>{c}</option>)}</select></label>
        <F k="risk_per_trade_pct" label="Risk / Trade %" />
        <F k="max_daily_loss_pct" label="Max Daily Loss %" />
        <F k="max_portfolio_exposure_pct" label="Max Exposure %" />
        <F k="max_drawdown_pct" label="Max Drawdown %" />
        <F k="max_concurrent_positions" label="Max Concurrent" />
        <F k="spread_bps" label="Spread (bps)" />
        <F k="slippage_bps" label="Slippage (bps)" />
        <F k="commission_per_share" label="Commission / Share" />
        <F k="min_commission" label="Min Commission" />
      </div>
      {errors.length ? <ul className="bt-errs">{errors.map((x) => <li key={x}>⚠ {x}</li>)}</ul> : null}
      <div className="bt-actions">
        <button type="submit" className="bt-run" disabled={busy || !connected}
          title={connected ? "Run an internal historical research backtest" : "Backend not connected"}>
          {busy ? "Running backtest…" : "Run Backtest"}
        </button>
        <span className="bt-safe">Research only · starts an internal historical run · never trades, never places an order</span>
      </div>
    </form>
  );
}
