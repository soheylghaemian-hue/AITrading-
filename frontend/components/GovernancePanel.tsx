"use client";
// AI DECISION GOVERNANCE panel (§ Phase G3.3) — the deterministic verdict on whether an AI assessment
// is trusted (APPROVED), incomplete (PARTIAL), contradictory (CONFLICT) or must not proceed (BLOCKED).
// It evaluates decision quality/readiness only — never a trade, order, or broker action. Loading →
// spinner; no verdict / backend down → NO DATA (never fabricated).
import React, { useEffect, useState } from "react";
import { fetchGovernance } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { govTone, hasGovernance, reasonText, type Governance } from "@/lib/governance";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}
function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="fmetric"><div className="label">{label}</div><div className="v num">{value}</div></div>;
}

export function GovernanceView({ data, loading, error }: { data: Governance | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Evaluating decision governance…" />;
  if (error) return <Box title={NO_DATA} note="Governance unavailable — nothing is shown, nothing invented." />;
  if (!hasGovernance(data))
    return <Box title={NO_DATA} note="No governance verdict yet. A verdict appears once the AI has an assessment for this symbol — never fabricated." />;

  const d = data as Governance;
  const tone = govTone(d.status);
  const pctv = (x: number | null) => (x == null ? NO_DATA : `${x}%`);

  return (
    <div className={`card consensus govcard ${tone}`}>
      <div className="chead">
        <div>
          <div className="label">AI Decision Governance</div>
          <div className="csym">{d.symbol}</div>
        </div>
        <div className="cbig">
          <span className={`govb ${tone}`}>{d.status}</span>
          <span className="cconf">{d.approved ? "Ready ✓" : "Not ready"}</span>
        </div>
      </div>

      <div className="fgrid" style={{ gridTemplateColumns: "repeat(3,1fr)" }}>
        <Stat label="Conviction" value={d.score == null ? NO_DATA : d.score} />
        <Stat label="Confidence" value={pctv(d.confidence)} />
        <Stat label="Data Completeness" value={pctv(d.data_completeness)} />
      </div>

      <div className="govdetail">
        {d.status === "APPROVED" ? (
          <div className="fitem pos">✓ All governance rules satisfied — assessment trusted &amp; ready.</div>
        ) : null}

        {d.status === "CONFLICT" ? (
          d.conflicts.length
            ? d.conflicts.map((c) => <div className="fitem neg" key={c}>⚠ {c}</div>)
            : <div className="fitem neg">⚠ Important intelligence sources disagree.</div>
        ) : null}

        {d.status === "PARTIAL" ? (
          <>
            <div className="label" style={{ marginBottom: 4 }}>Missing / below threshold</div>
            {d.reasons.length
              ? d.reasons.map((r) => <div className="fitem warn" key={r}>· {reasonText(r)}</div>)
              : <div className="fitem neu">{NO_DATA}</div>}
          </>
        ) : null}

        {d.status === "BLOCKED" ? (
          <>
            <div className="fitem neg">⛔ Assessment should not proceed.</div>
            {d.reasons.map((r) => <div className="fitem neg" key={r}>· {reasonText(r)}</div>)}
          </>
        ) : null}
      </div>
    </div>
  );
}

export function GovernancePanel({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: Governance | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchGovernance(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <GovernanceView data={state.data} loading={state.loading} error={state.error} />;
}
