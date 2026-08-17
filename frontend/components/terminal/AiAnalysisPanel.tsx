// AI Market Analysis / Explanation (pure): Conviction · Assessment (direction, drivers, risks,
// conflicts, missing inputs) · Readiness (governance, risk, completeness, execution) · Decision ·
// Conviction inputs · WHY agent breakdown. It surfaces DISAGREEMENT between inputs and is never
// simplified into a single "BUY" — an AI score is intelligence, not a trade command. Every missing
// field renders NO DATA. Read-only: no trade/order control, never enables execution.
import React from "react";
import { NO_DATA, isPresent, money } from "@/lib/format";
import { Tag } from "../ui";
import { directionTone, type AiConsensus } from "@/lib/consensus";
import { govTone, reasonText, type Governance } from "@/lib/governance";
import type { Completeness } from "@/lib/completeness";

const AGENTS: [string, string][] = [
  ["momentum", "Momentum Agent"],
  ["risk", "Risk Agent"],
  ["macro", "Macro Agent"],
  ["news", "News Agent"],
];

export function AiAnalysisPanel({ dec, risk, mode, executionEnabled, convictionInputs,
  consensus, governance, riskStatus, completeness }: {
  dec: Record<string, any> | null;
  risk: Record<string, any> | null;
  mode?: string;
  executionEnabled?: boolean;
  // §G2.5 AI Brain: the intelligence inputs feeding a future conviction model. Each is a real 0-100
  // signal or NO DATA — NO overall conviction is fabricated from partial inputs.
  convictionInputs?: { label: string; value: number | null }[];
  // §UX-1 explanation context — direction/drivers/risks/conflicts (consensus), governance verdict,
  // Risk Control state and data completeness. All optional and NO DATA when absent.
  consensus?: AiConsensus | null;
  governance?: Governance | null;
  riskStatus?: string | null;
  completeness?: Completeness | null;
}) {
  const conf = dec?.confidence;
  const score = isPresent(conf) ? Math.round(conf * 100) : null;
  const action = (dec?.action || "").toString().toUpperCase();
  const verdict = (dec?.risk_decision || "").toString().toUpperCase();
  const agents = dec?.agents as Record<string, any> | undefined;
  const execMode = `${(mode || "PAPER").toUpperCase()} · ${executionEnabled ? "ENABLED" : "DISABLED"}`;
  const drivers = consensus?.strengths?.slice(0, 3) ?? [];
  const risks = consensus?.risks?.slice(0, 2) ?? [];
  const conflicts = consensus?.conflicts?.slice(0, 2) ?? [];
  const missing = governance?.missing ?? [];
  const riskTone = riskStatus === "READY" ? "ok" : riskStatus === "BLOCKED" ? "sell"
    : riskStatus === "WARNING" ? "warnt" : "muted";

  return (
    <div className="card ai">
      <h3>AI Explanation</h3>

      <div className="sec">
        <div className="label" style={{ marginBottom: 8 }}>AI Conviction</div>
        {score === null ? <span className="num neut">{NO_DATA}</span> : (
          <div className="score"><span className={`big ${action === "SELL" ? "down" : "up"}`}>{score}</span><span className="of">/ 100</span>
            <div className="confbar" style={{ maxWidth: 120 }}><i style={{ width: `${score}%` }} /></div></div>
        )}
      </div>

      {/* Assessment — direction + drivers + risks + conflicts + missing inputs. Disagreement is shown. */}
      <div className="sec">
        <div className="label" style={{ marginBottom: 8 }}>Assessment</div>
        <div className="row"><span className="k">Direction</span>
          {consensus?.direction ? <span className={`consb ${directionTone(consensus.direction)}`}>{consensus.direction}</span> : <span className="num neut">{NO_DATA}</span>}</div>
        <div className="ai-expl">
          <div className="label">Drivers</div>
          {drivers.length ? <ul className="ai-list pos">{drivers.map((d) => <li key={d}>✓ {d}</li>)}</ul> : <span className="num neut">{NO_DATA}</span>}
          <div className="label" style={{ marginTop: 8 }}>Risks</div>
          {risks.length ? <ul className="ai-list neg">{risks.map((rk) => <li key={rk}>⚠ {rk}</li>)}</ul> : <span className="num neut">{NO_DATA}</span>}
          {conflicts.length ? (<><div className="label" style={{ marginTop: 8 }}>Conflicts</div>
            <ul className="ai-list warn">{conflicts.map((cf) => <li key={cf}>⇄ {cf}</li>)}</ul></>) : null}
          {missing.length ? (<><div className="label" style={{ marginTop: 8 }}>Missing inputs</div>
            <div className="ai-missing">{missing.join(", ")}</div></>) : null}
        </div>
      </div>

      {/* Readiness — governance, risk, completeness, execution. Never simplified into a single verdict. */}
      <div className="sec">
        <div className="label" style={{ marginBottom: 8 }}>Readiness</div>
        <div className="row"><span className="k">Governance</span>{governance?.status ? <Tag kind={govTone(governance.status) === "approved" ? "ok" : govTone(governance.status) === "blocked" ? "sell" : "warnt"}>{governance.status}</Tag> : <span className="num neut">{NO_DATA}</span>}</div>
        <div className="row"><span className="k">Risk</span><Tag kind={riskTone as any}>{riskStatus ?? NO_DATA}</Tag></div>
        <div className="row"><span className="k">Data Completeness</span><b className="num">{completeness?.score == null ? NO_DATA : `${completeness.score}/100`}</b></div>
        <div className="row"><span className="k">Execution</span><Tag kind={executionEnabled ? "warnt" : "muted"}>{execMode}</Tag></div>
      </div>

      <div className="sec">
        <div className="row"><span className="k">Decision</span>{action ? <Tag kind={action === "BUY" ? "buy" : action === "SELL" ? "sell" : "muted"}>{action}</Tag> : <span className="num neut">{NO_DATA}</span>}</div>
        <div className="row"><span className="k">Confidence</span><b className="num">{score === null ? NO_DATA : score + "%"}</b></div>
        <div className="row"><span className="k">Risk Engine</span>{verdict ? <Tag kind={verdict === "APPROVED" ? "ok" : verdict === "REJECTED" ? "sell" : "muted"}>{verdict}</Tag> : <span className="num neut">{NO_DATA}</span>}</div>
        <div className="row"><span className="k">Execution Mode</span><Tag kind="muted">{execMode}</Tag></div>
      </div>

      <div className="sec">
        <div className="row"><span className="k">Position Risk</span><b className="num">{money(dec?.monetary_risk)}</b></div>
        <div className="row"><span className="k">Max Daily Loss</span><b className="num">{risk ? money(risk.max_daily_loss, 0) : NO_DATA}</b></div>
        <div className="row"><span className="k">Remaining Today</span><b className="num up">{risk ? money(risk.remaining_daily_risk, 0) : NO_DATA}</b></div>
      </div>

      {convictionInputs && convictionInputs.length ? (
        <div className="sec">
          <div className="label" style={{ marginBottom: 8 }}>Conviction Inputs</div>
          {convictionInputs.map((ci) => (
            <div className="cinput" key={ci.label}>
              <span className="k">{ci.label}</span>
              {ci.value == null ? <span className="num neut">{NO_DATA}</span> : (
                <span className="cival"><i style={{ width: `${Math.max(0, Math.min(100, ci.value))}%` }} /><b className="num">{Math.round(ci.value)}</b></span>
              )}
            </div>
          ))}
        </div>
      ) : null}

      <div className="sec">
        <details><summary><span className="chev">▸</span>WHY? — agent breakdown</summary>
          {AGENTS.map(([k, label]) => {
            const v = agents?.[k];
            return <div className="agent" key={k}><b>{label}</b>{v != null ? <Tag kind="muted">{String(v)}</Tag> : <span className="num neut">{NO_DATA}</span>}</div>;
          })}
        </details>
      </div>
    </div>
  );
}
