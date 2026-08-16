// AI Market Analysis (pure): Conviction · Decision · Confidence · Risk Engine · Execution Mode + WHY
// (Momentum / Risk / Macro / News agents). Every missing field renders NO DATA — never invented.
import React from "react";
import { NO_DATA, isPresent, money } from "@/lib/format";
import { Tag } from "../ui";

const AGENTS: [string, string][] = [
  ["momentum", "Momentum Agent"],
  ["risk", "Risk Agent"],
  ["macro", "Macro Agent"],
  ["news", "News Agent"],
];

export function AiAnalysisPanel({ dec, risk, mode, executionEnabled, convictionInputs }: {
  dec: Record<string, any> | null;
  risk: Record<string, any> | null;
  mode?: string;
  executionEnabled?: boolean;
  // §G2.5 AI Brain: the intelligence inputs feeding a future conviction model. Each is a real 0-100
  // signal or NO DATA — NO overall conviction is fabricated from partial inputs.
  convictionInputs?: { label: string; value: number | null }[];
}) {
  const conf = dec?.confidence;
  const score = isPresent(conf) ? Math.round(conf * 100) : null;
  const action = (dec?.action || "").toString().toUpperCase();
  const verdict = (dec?.risk_decision || "").toString().toUpperCase();
  const agents = dec?.agents as Record<string, any> | undefined;
  const execMode = `${(mode || "PAPER").toUpperCase()} · ${executionEnabled ? "ENABLED" : "DISABLED"}`;

  return (
    <div className="card ai">
      <h3>AI Market Analysis</h3>

      <div className="sec">
        <div className="label" style={{ marginBottom: 8 }}>AI Conviction</div>
        {score === null ? <span className="num neut">{NO_DATA}</span> : (
          <div className="score"><span className={`big ${action === "SELL" ? "down" : "up"}`}>{score}</span><span className="of">/ 100</span>
            <div className="confbar" style={{ maxWidth: 120 }}><i style={{ width: `${score}%` }} /></div></div>
        )}
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
