"use client";
// Risk Control configuration form (§ Phase R2.0). Edits the capital-protection LIMITS only —
// capital, per-trade risk, daily loss, exposure, drawdown and the warning threshold. It is
// authenticated by the server proxy (owner token injected server-side) and uses optimistic
// concurrency (the version token from the current config) so a stale or out-of-band change is
// rejected. Client-side validation mirrors the backend; the backend re-validates authoritatively.
//
// SAFETY: changing these limits does NOT enable trading, does NOT place or submit an order, does NOT
// touch the broker / IBKR / execution, and does NOT arm or disarm the kill switch.
import React, { useState } from "react";
import type { RiskConfig, RiskConfigView } from "@/lib/risk";
import { updateRiskConfig, type RiskConfigUpdate } from "@/lib/api";

const CURRENCIES = ["EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD"];

type Fields = {
  capital: string; currency: string; max_daily_loss_pct: string; max_position_risk_pct: string;
  max_portfolio_exposure_pct: string; max_drawdown_pct: string; warning_threshold_pct: string;
};

function fieldsFrom(cfg: RiskConfig | null): Fields {
  return {
    capital: cfg?.capital != null ? String(cfg.capital) : "",
    currency: cfg?.currency ?? "USD",
    max_daily_loss_pct: cfg?.max_daily_loss_pct != null ? String(cfg.max_daily_loss_pct) : "",
    max_position_risk_pct: cfg?.max_position_risk_pct != null ? String(cfg.max_position_risk_pct) : "",
    max_portfolio_exposure_pct: cfg?.max_portfolio_exposure_pct != null ? String(cfg.max_portfolio_exposure_pct) : "",
    max_drawdown_pct: cfg?.max_drawdown_pct != null ? String(cfg.max_drawdown_pct) : "",
    warning_threshold_pct: cfg?.warning_threshold_pct != null ? String(cfg.warning_threshold_pct) : "80",
  };
}

// Client-side mirror of the backend bounds (backend re-validates; this is just fast feedback).
export function validate(f: Fields): { errors: string[]; parsed?: RiskConfigUpdate } {
  const errors: string[] = [];
  const n = (s: string) => (s.trim() === "" ? NaN : Number(s));
  const cap = n(f.capital), mdl = n(f.max_daily_loss_pct), mpr = n(f.max_position_risk_pct);
  const mpe = n(f.max_portfolio_exposure_pct), mdd = n(f.max_drawdown_pct), warn = n(f.warning_threshold_pct);
  if (!(cap > 0)) errors.push("Capital must be greater than 0");
  if (!CURRENCIES.includes(f.currency)) errors.push("Unsupported currency");
  const pctRange = (v: number, label: string, max = 100) => {
    if (!Number.isFinite(v) || v <= 0) errors.push(`${label} must be greater than 0`);
    else if (v > max) errors.push(`${label} must be ≤ ${max}%`);
  };
  pctRange(mdl, "Max daily loss");
  pctRange(mpr, "Max per-trade risk");
  if (!Number.isFinite(mpe) || mpe <= 0) errors.push("Max portfolio exposure must be greater than 0");
  pctRange(mdd, "Max drawdown");
  if (!Number.isFinite(warn) || warn <= 0 || warn >= 100) errors.push("Warning threshold must be between 0 and 100%");
  if (errors.length) return { errors };
  return {
    errors: [],
    parsed: {
      capital: cap, currency: f.currency, max_daily_loss_pct: mdl, max_position_risk_pct: mpr,
      max_portfolio_exposure_pct: mpe, max_drawdown_pct: mdd, warning_threshold_pct: warn,
    },
  };
}

export function RiskConfigForm({
  view, connected, onSaved,
}: { view: RiskConfigView | null; connected: boolean; onSaved?: () => void }) {
  const [f, setF] = useState<Fields>(() => fieldsFrom(view?.config ?? null));
  const [errors, setErrors] = useState<string[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const set = (k: keyof Fields) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF((p) => ({ ...p, [k]: e.target.value }));

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    const { errors: errs, parsed } = validate(f);
    setErrors(errs);
    if (errs.length || !parsed) return;
    const ok = window.confirm(
      "APPLY RISK LIMITS\n\n" +
      "This updates your capital-protection limits only. It does NOT enable trading, does NOT place any " +
      "order, and does NOT change the kill switch.\n\nApply these limits?");
    if (!ok) return;
    setBusy(true);
    try {
      const r = await updateRiskConfig({ ...parsed, expected_version: view?.version_token ?? null });
      if (r.ok) { setMsg({ ok: true, text: "Limits updated. Trading is NOT enabled." }); onSaved?.(); }
      else if (r.conflict) setMsg({ ok: false, text: "Configuration changed elsewhere — reload and retry." });
      else if (r.errors?.length) setErrors(r.errors);
      else setMsg({ ok: false, text: r.detail || "Update failed." });
    } finally { setBusy(false); }
  }

  return (
    <form className="card riskform" onSubmit={onSubmit}>
      <button type="button" className="rf-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="chev">{open ? "▾" : "▸"}</span> Edit risk limits
      </button>
      {open ? (
        <>
          <p className="rf-note">
            Changing these limits does <b>not</b> enable trading and never places an order. It only updates
            the capital-protection thresholds the system observes.
          </p>
          <div className="rf-grid">
            <label className="rf-f"><span className="label">Trading capital</span>
              <input className="num" inputMode="decimal" value={f.capital} onChange={set("capital")} placeholder="100000" /></label>
            <label className="rf-f"><span className="label">Currency</span>
              <select value={f.currency} onChange={set("currency")}>{CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}</select></label>
            <label className="rf-f"><span className="label">Max per-trade risk %</span>
              <input className="num" inputMode="decimal" value={f.max_position_risk_pct} onChange={set("max_position_risk_pct")} placeholder="1" /></label>
            <label className="rf-f"><span className="label">Max daily loss %</span>
              <input className="num" inputMode="decimal" value={f.max_daily_loss_pct} onChange={set("max_daily_loss_pct")} placeholder="2" /></label>
            <label className="rf-f"><span className="label">Max portfolio exposure %</span>
              <input className="num" inputMode="decimal" value={f.max_portfolio_exposure_pct} onChange={set("max_portfolio_exposure_pct")} placeholder="100" /></label>
            <label className="rf-f"><span className="label">Max drawdown %</span>
              <input className="num" inputMode="decimal" value={f.max_drawdown_pct} onChange={set("max_drawdown_pct")} placeholder="20" /></label>
            <label className="rf-f"><span className="label">Warning threshold %</span>
              <input className="num" inputMode="decimal" value={f.warning_threshold_pct} onChange={set("warning_threshold_pct")} placeholder="80" /></label>
          </div>
          {errors.length ? (
            <ul className="rf-errs">{errors.map((x) => <li key={x}>⚠ {x}</li>)}</ul>
          ) : null}
          {msg ? <div className={`rf-msg ${msg.ok ? "ok" : "err"}`}>{msg.text}</div> : null}
          <div className="rf-actions">
            <button type="submit" className="rf-apply" disabled={busy || !connected}
              title={connected ? "Apply the limits (requires confirmation)" : "Backend not connected"}>
              {busy ? "Applying…" : "Apply limits"}
            </button>
            <span className="rf-safe">Does not enable trading · no order is placed</span>
          </div>
        </>
      ) : null}
    </form>
  );
}
