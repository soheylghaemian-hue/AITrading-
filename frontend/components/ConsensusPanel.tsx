"use client";
// GIGBAY AI CONVICTION panel (§ Phase G3) — the orchestrated AI market view: conviction score,
// direction, confidence, per-source components, strengths, risks and surfaced CONFLICTS. Intelligence
// signal only — never a trading decision. Loading → spinner; error / no coverage → NO DATA.
import React, { useEffect, useState } from "react";
import { fetchConsensus } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { directionTone, hasConsensus, scoreTier, type AiConsensus } from "@/lib/consensus";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}

export function ConsensusView({ data, loading, error }: { data: AiConsensus | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Computing the AI market view…" />;
  if (error) return <Box title={NO_DATA} note="AI consensus unavailable — nothing is shown, nothing invented." />;
  if (!hasConsensus(data))
    return <Box title={NO_DATA} note="No intelligence available for this symbol yet. The AI market view appears once the underlying layers have data — never fabricated." />;

  const d = data as AiConsensus;
  const partial = d.status === "PARTIAL";

  return (
    <div className="card consensus">
      <div className="chead">
        <div>
          <div className="label">GIGBAY AI Conviction</div>
          <div className="csym">{d.symbol}</div>
        </div>
        <div className="cbig">
          <span className={`cscore ${scoreTier(d.score)}`}>{d.score == null ? NO_DATA : d.score}<small>{d.score == null ? "" : " / 100"}</small></span>
          <span className={`consb ${directionTone(d.direction)}`}>{d.direction || NO_DATA}</span>
          <span className="cconf">Confidence <b className="num">{d.confidence == null ? NO_DATA : `${d.confidence}%`}</b></span>
          {partial ? <span className="cpartial">PARTIAL ASSESSMENT</span> : null}
        </div>
      </div>

      {d.conflicts.length ? (
        <div className="cconflict">
          <b>⚠ CONFLICT DETECTED</b>
          <div className="cconflict-list">{d.conflicts.map((c) => <span key={c}>· {c}</span>)}</div>
        </div>
      ) : null}

      <div className="ccomp">
        <div className="label" style={{ marginBottom: 8 }}>Components</div>
        {d.components.map((c) => (
          <div className="crow" key={c.component_name}>
            <span className="cname">{c.component_name}</span>
            <span className={`cbar-wrap`}><i className={directionTone(c.direction)} style={{ width: `${c.score ?? 0}%` }} /></span>
            <span className={`cval num ${directionTone(c.direction)}`}>{c.score == null ? NO_DATA : c.score}</span>
            <span className="cwt">{Math.round(c.weight * 100)}%</span>
          </div>
        ))}
      </div>

      <div className="fsr">
        <div className="fcol">
          <div className="label">Strengths</div>
          {d.strengths.length ? d.strengths.map((s) => <div className="fitem pos" key={s}>✓ {s}</div>)
            : <div className="fitem neu">{NO_DATA}</div>}
        </div>
        <div className="fcol">
          <div className="label">Risks</div>
          {d.risks.length ? d.risks.map((s) => <div className="fitem neg" key={s}>⚠ {s}</div>)
            : <div className="fitem neu">{NO_DATA}</div>}
        </div>
      </div>
    </div>
  );
}

export function ConsensusPanel({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: AiConsensus | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchConsensus(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <ConsensusView data={state.data} loading={state.loading} error={state.error} />;
}
