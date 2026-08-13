"use client";

import React, { useState } from "react";
import type { Snapshot } from "../lib/types";
import { setRiskConfig } from "../lib/api";
import { money, pct, sign, isPresent, NO_DATA } from "../lib/format";

// The TRADING RISK panel: exactly THREE user parameters. Everything else (position size,
// leverage, exposure) is computed by the Position Sizer / Risk Engine and may never exceed these.
// Saving POSTs (token-authenticated) to the backend, which applies it to the authoritative Risk
// Engine. This never enables execution and never touches the broker directly.
export function TradingRisk({ s }: { s: Snapshot | null }) {
  const tr = s?.trading_risk ?? null;
  const [capital, setCapital] = useState<string>("");
  const [riskPct, setRiskPct] = useState<string>("");
  const [dailyPct, setDailyPct] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>("");

  // Seed the form from the live config once it arrives (and the user hasn't typed yet).
  const seeded = React.useRef(false);
  React.useEffect(() => {
    if (tr && !seeded.current) {
      seeded.current = true;
      setCapital(String(tr.capital));
      setRiskPct(String((tr.risk_per_trade_pct * 100).toFixed(2)));
      setDailyPct(String((tr.max_daily_loss_pct * 100).toFixed(2)));
    }
  }, [tr]);

  const capN = Number(capital), riskN = Number(riskPct), dailyN = Number(dailyPct);
  const validNums = isPresent(capN) && capN > 0 && isPresent(riskN) && riskN > 0 && isPresent(dailyN) && dailyN > 0;
  const previewTrade = validNums ? capN * (riskN / 100) : null;
  const previewDaily = validNums ? capN * (dailyN / 100) : null;
  const riskGtDaily = validNums && riskN > dailyN;

  async function onSave() {
    if (!validNums) { setMsg("Enter capital > 0 and both percentages > 0."); return; }
    if (riskGtDaily) { setMsg("Risk per Trade may not exceed Max Daily Loss."); return; }
    const token = prompt("Owner token (ATP_DASHBOARD_TOKEN) — sent only to the backend:") || "";
    if (!token) return;
    setBusy(true);
    const res = await setRiskConfig(token, {
      capital: capN, risk_per_trade_pct: riskN / 100, max_daily_loss_pct: dailyN / 100,
    });
    setBusy(false);
    setMsg(res.ok ? "Saved — applied to the Risk Engine." : `Not applied: ${res.detail}`);
  }

  const statusReached = tr?.status === "DAILY LOSS LIMIT REACHED";
  const statusLabel = tr ? (statusReached ? `🔴 ${tr.status}` : tr.status) : NO_DATA;
  return (
    <section className="card">
      <h2>
        <span>Trading risk · live configuration</span>
        {tr ? <span className={`pill p-${statusReached ? "halted" : "armed"}`}>{statusLabel}</span>
            : <span className="pill p-no_data">NO DATA</span>}
      </h2>

      {/* Derived, live values from the authoritative Risk Engine */}
      <div className="health">
        <div className="h"><span className="n">Trading capital</span><span className="mono">{money(tr?.capital, 0)}</span></div>
        <div className="h"><span className="n">Risk per trade</span><span className="mono">{tr ? pct(tr.risk_per_trade_pct) : NO_DATA}</span></div>
        <div className="h"><span className="n">Max risk / trade ($)</span><span className="mono">{money(tr?.max_risk_per_trade, 0)}</span></div>
        <div className="h"><span className="n">Daily loss limit</span><span className="mono">{tr ? pct(tr.max_daily_loss_pct) : NO_DATA}</span></div>
        <div className="h"><span className="n">Max daily loss ($)</span><span className="mono">{money(tr?.max_daily_loss, 0)}</span></div>
        <div className="h"><span className="n">Today's P&L</span><span className={`mono ${sign(tr?.current_daily_pnl)}`}>{money(tr?.current_daily_pnl, 0)}</span></div>
        <div className="h"><span className="n">Remaining daily loss</span><span className="mono">{money(tr?.remaining_daily_risk, 0)}</span></div>
        <div className="h"><span className="n">Trading status</span>
          <span className={`pill p-${statusReached ? "halted" : tr ? "armed" : "no_data"}`}>{statusLabel}</span></div>
      </div>

      {/* The only three controls. Position size / leverage / exposure are computed automatically. */}
      <div style={{ padding: "14px 16px", borderTop: "1px solid var(--border)" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))", gap: 12 }}>
          <Field label="Trading capital ($)" value={capital} onChange={setCapital} step="1000" min="0" />
          <Field label="Risk per trade (%)" value={riskPct} onChange={setRiskPct} step="0.1" min="0" />
          <Field label="Max daily loss (%)" value={dailyPct} onChange={setDailyPct} step="0.1" min="0" />
        </div>
        <div className="mono" style={{ color: "var(--muted2)", fontSize: 12, marginTop: 10 }}>
          Preview → max risk/trade {previewTrade == null ? NO_DATA : money(previewTrade, 0)} ·
          {" "}max daily loss {previewDaily == null ? NO_DATA : money(previewDaily, 0)}
          {riskGtDaily ? "  ⚠ risk/trade exceeds daily loss" : ""}
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 12 }}>
          <button className="estop" style={{ background: "linear-gradient(180deg,#1f6feb,#1751b8)", borderColor: "#3b82f6" }}
            onClick={onSave} disabled={busy || !validNums || riskGtDaily}>
            {busy ? "Saving…" : "Apply to Risk Engine"}
          </button>
          {msg ? <span className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>{msg}</span> : null}
        </div>
      </div>

      <div className="banner ok" style={{ borderTop: "1px solid var(--border)" }}>
        Only these three parameters are user-set. The Position Sizing Engine derives trade quantity
        from signal quality, stop distance, volatility, liquidity and capital, and the Risk Engine
        vetoes anything exceeding Risk-per-Trade and blocks all new trades once the daily-loss limit
        is reached. This does not enable execution or live trading.
      </div>
    </section>
  );
}

function Field({ label, value, onChange, step, min }: {
  label: string; value: string; onChange: (v: string) => void; step: string; min: string;
}) {
  return (
    <label style={{ display: "block" }}>
      <span style={{ display: "block", color: "var(--muted)", fontSize: 10, textTransform: "uppercase", letterSpacing: ".6px", marginBottom: 6 }}>{label}</span>
      <input type="number" inputMode="decimal" step={step} min={min} value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: "100%", background: "var(--panel2)", border: "1px solid var(--border2)", borderRadius: 8,
          color: "var(--text)", padding: "9px 11px", fontFamily: "var(--mono)", fontSize: 14 }} />
    </label>
  );
}
