"use client";
// SMART MONEY FLOW panel (§ Phase R1.3) — institutional intelligence for a symbol in the Market
// Terminal: 13F quarter-over-quarter position changes (ACCUMULATION / REDUCTION / NEW / EXIT) + an
// accumulation score, and SEC Form 4 insider BUY/SELL sentiment. Read-only intelligence — never a trade
// or copy-trade. No data → NO DATA (never fabricated).
import React from "react";
import { NO_DATA } from "@/lib/format";
import { clusterLabel, clusterTone, fmtShares, flowTone, hasInstitutional, type InstitutionalFlow } from "@/lib/institutional";

const DIR_LABEL: Record<string, string> = {
  ACCUMULATION: "▲ ACC", NEW_POSITION: "✦ NEW", REDUCTION: "▼ RED", EXIT: "✕ EXIT",
};

export function InstitutionalPanel({ data }: { data: InstitutionalFlow | null }) {
  if (!hasInstitutional(data)) {
    return (
      <div className="card smf">
        <div className="smf-head"><span className="label">Smart Money Flow</span><span className="smf-nd">{NO_DATA}</span></div>
        <p className="smf-note">No institutional data yet — 13F changes + Form 4 insiders appear once the provider is active. Never fabricated.</p>
      </div>
    );
  }
  const d = data as InstitutionalFlow;
  const instTone = flowTone(d.institutional_direction);
  const netPct = d.net_share_change_pct;
  const cl = d.insider_cluster;
  const clTone = clusterTone(cl?.cluster_type);

  return (
    <div className={`card smf ${instTone}`}>
      <div className="smf-head">
        <span className="label">Smart Money Flow</span>
        <span className="smf-sym">{d.symbol}</span>
      </div>

      <div className="smf-summary">
        <div className="smf-metric">
          <div className="label">Institutional</div>
          <div className={`smf-badge ${instTone}`}>{d.institutional_direction ?? NO_DATA}</div>
        </div>
        <div className="smf-metric">
          <div className="label">13F Net Change</div>
          <div className={`smf-val num ${netPct == null ? "" : netPct >= 0 ? "up" : "down"}`}>
            {netPct == null ? NO_DATA : `${netPct >= 0 ? "+" : ""}${netPct}%`}</div>
        </div>
        <div className="smf-metric">
          <div className="label">Insider Cluster</div>
          <div className={`smf-badge ${clTone}`}>{clusterLabel(cl?.cluster_type)}</div>
        </div>
        <div className="smf-metric">
          <div className="label">Cluster Score</div>
          <div className="smf-val num">{cl?.score == null ? NO_DATA : `${cl.score}/100`}</div>
        </div>
      </div>

      {cl?.summary ? <p className="smf-clustersum">🔎 {cl.summary}</p> : null}

      <div className="smf-lists">
        <div className="smf-col">
          <div className="label">13F Position Changes</div>
          {d.institutional_changes.length
            ? d.institutional_changes.slice(0, 5).map((c, i) => (
                <div className={`smf-row ${flowTone(c.direction)}`} key={`${c.institution}-${i}`}>
                  <span className="smf-inst">{c.institution}</span>
                  <span className={`smf-dir ${flowTone(c.direction)}`}>{DIR_LABEL[c.direction ?? ""] ?? c.direction}</span>
                  <span className="smf-pct num">{c.percentage_change == null ? "new" : `${c.percentage_change >= 0 ? "+" : ""}${c.percentage_change}%`}</span>
                  <span className="smf-sh num">{fmtShares(c.current_shares)}</span>
                </div>))
            : <div className="smf-empty">{NO_DATA}</div>}
        </div>
        <div className="smf-col">
          <div className="label">Insider Activity <span className="smf-count">({d.insider_summary.buy_count} buy / {d.insider_summary.sell_count} sell)</span></div>
          {d.insider_activity.length
            ? d.insider_activity.slice(0, 5).map((t, i) => (
                <div className={`smf-row ${t.transaction_type === "BUY" ? "acc" : "red"}`} key={`${t.insider_name}-${i}`}>
                  <span className="smf-inst">{t.insider_name ?? "—"}</span>
                  <span className={`smf-dir ${t.transaction_type === "BUY" ? "acc" : "red"}`}>{t.transaction_type}</span>
                  <span className="smf-sh num">{fmtShares(t.shares)}</span>
                  <span className="smf-px num">{t.price == null ? "" : `$${t.price}`}</span>
                </div>))
            : <div className="smf-empty">{NO_DATA}</div>}
        </div>
      </div>
    </div>
  );
}
