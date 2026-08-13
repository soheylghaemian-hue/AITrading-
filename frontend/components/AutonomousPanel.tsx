"use client";

import React, { useState } from "react";
import type { Snapshot } from "../lib/types";
import { autonomousControl } from "../lib/api";
import { Autonomous } from "./sections2";

// Client wrapper: the read-only AUTONOMOUS TRADING panel + the token-gated control buttons.
// Two-step activation: ARM, then START PAPER AUTONOMOUS with an explicit confirmation. Never
// enables live trading; every control POSTs to the authenticated backend (Risk Engine authoritative).
export function AutonomousPanel({ s }: { s: Snapshot | null }) {
  const status = s?.autonomous?.status ?? "DISABLED";
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function control(action: "arm" | "disarm" | "dry_run" | "start" | "stop" | "kill" | "reset") {
    if (action === "start") {
      if (!confirm("START PAPER AUTONOMOUS TRADING — the system will place INTERNAL paper orders (no live, no IBKR). Continue?")) return;
      const phrase = prompt('Type exactly to confirm:\n\nYES, START PAPER TRADING') || "";
      if (phrase !== "YES, START PAPER TRADING") { setMsg("Start cancelled — confirmation phrase did not match."); return; }
    }
    if (action === "kill") {
      if (!confirm("KILL SWITCH — block ALL paper trading until an explicit reset. Continue?")) return;
    }
    const token = prompt("Owner token (ATP_DASHBOARD_TOKEN) — sent only to the backend:") || "";
    if (!token) return;
    setBusy(true);
    const payload = action === "start" ? { confirm: true } : {};
    const res = await autonomousControl(action, token, payload);
    setBusy(false);
    setMsg(res.ok ? `OK — status: ${res.detail}` : `Not applied: ${res.detail}`);
  }

  const btn = (label: string, action: any, kind = "") => (
    <button className="estop" disabled={busy}
      style={{ background: kind === "danger" ? "linear-gradient(180deg,#f85149,#c9302c)"
             : kind === "go" ? "linear-gradient(180deg,#3fb950,#2ea043)"
             : "linear-gradient(180deg,#1f6feb,#1751b8)", borderColor: "#3b82f6" }}
      onClick={() => control(action)}>{label}</button>
  );

  return (
    <>
      <Autonomous s={s} />
      <section className="card">
        <h2>Autonomous controls · two-step activation (paper only)</h2>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", padding: "14px 16px", alignItems: "center" }}>
          {btn("① ARM", "arm")}
          {btn("Dry run", "dry_run")}
          {btn("② START PAPER AUTONOMOUS", "start", "go")}
          {btn("Stop", "stop")}
          {btn("Disarm", "disarm")}
          {btn("■ Kill switch", "kill", "danger")}
          {status === "KILLED" ? btn("Reset kill", "reset") : null}
          {msg ? <span className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>{msg}</span> : null}
        </div>
        <div className="banner ok" style={{ borderTop: "1px solid var(--border)" }}>
          ARM computes and logs AI decisions but places NO orders. START (with confirmation) runs
          INTERNAL paper execution only — never IBKR, never live. The Risk Engine vetoes every trade
          and the daily-loss lock / kill switch are authoritative. All controls require the owner token.
        </div>
      </section>
    </>
  );
}
