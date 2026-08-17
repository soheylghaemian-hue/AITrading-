"use client";
// Compact Risk Control read-out (§ Phase R2.0) for the Market Intelligence Terminal. Shows the
// capital-protection state (READY / WARNING / BLOCKED / NO DATA), the daily-loss budget usage and
// the kill switch at a glance, with a link to the full Risk Center. Read-only: it never trades,
// never places an order, and never mutates the kill switch. Missing data → NO DATA (never zero).
import React from "react";
import { NO_DATA } from "@/lib/format";
import { type RiskStatus, stateTone, pctNum, reasonLabel, hasRiskState } from "@/lib/risk";

export function RiskCard({ data }: { data: RiskStatus | null }) {
  if (!hasRiskState(data)) {
    return (
      <div className="card riskcard">
        <div className="rc-head"><span className="label">Risk Control</span><span className="rc-nd">{NO_DATA}</span></div>
        <p className="rc-note">No risk state yet — appears once a risk configuration and live inputs exist. Never fabricated.</p>
      </div>
    );
  }
  const d = data as RiskStatus;
  const tone = stateTone(d.status as string);
  const used = d.daily_pnl.used_pct;
  return (
    <div className={`card riskcard ${tone}`}>
      <div className="rc-head">
        <span className="label">Risk Control</span>
        <span className={`rc-state ${tone}`}>{d.status}</span>
        <span className={`rc-kill ${d.kill_switch === "STOPPED" ? "down" : "up"}`}>Kill: {d.kill_switch ?? NO_DATA}</span>
        <span className="rc-used num">Budget {pctNum(used)}</span>
      </div>
      {d.reasons.length ? (
        <div className="rc-reasons">{d.reasons.slice(0, 3).map((rc) => <span className="rc-r" key={rc}>{tone === "blocked" ? "⛔" : "⚠"} {reasonLabel(rc)}</span>)}</div>
      ) : null}
      <a className="rc-link" href="/risk">Open Risk Center →</a>
    </div>
  );
}
