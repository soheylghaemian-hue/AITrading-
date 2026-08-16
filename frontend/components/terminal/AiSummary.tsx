// AI VIEW summary card (§ Phase G3) for the Market Terminal — a compact read of the orchestrated AI
// consensus: direction, confidence, main drivers and main risks. Pure; NO DATA when the consensus is
// unavailable. Intelligence signal only, never a trading decision.
import React from "react";
import { NO_DATA } from "@/lib/format";
import { directionTone, hasConsensus, type AiConsensus } from "@/lib/consensus";

export function AiSummary({ data }: { data: AiConsensus | null }) {
  const ok = hasConsensus(data);
  const d = data as AiConsensus;
  const drivers = ok ? d.strengths.slice(0, 3) : [];
  const topRisk = ok && d.risks.length ? d.risks[0] : null;
  return (
    <div className="card aiview">
      <div className="aiview-head">
        <span className="label">AI View</span>
        {ok ? <span className={`consb ${directionTone(d.direction)}`}>{d.direction || NO_DATA}</span>
          : <span className="num neut">{NO_DATA}</span>}
        {ok ? <span className="aiview-conf">Confidence <b className="num">{d.confidence == null ? NO_DATA : `${d.confidence}%`}</b></span> : null}
        {ok && d.status === "PARTIAL" ? <span className="cpartial">PARTIAL</span> : null}
        {ok && d.conflicts.length ? <span className="cpartial neg">CONFLICT</span> : null}
      </div>
      <div className="aiview-body">
        <div>
          <div className="label">Main Drivers</div>
          {drivers.length ? <ol className="aiview-drivers">{drivers.map((s) => <li key={s}>{s}</li>)}</ol>
            : <span className="num neut">{NO_DATA}</span>}
        </div>
        <div>
          <div className="label">Main Risk</div>
          <div className="aiview-risk">{topRisk ? <span className="fitem neg">⚠ {topRisk}</span> : <span className="num neut">{NO_DATA}</span>}</div>
        </div>
      </div>
    </div>
  );
}
