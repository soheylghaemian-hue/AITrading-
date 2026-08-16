"use client";
// DATA QUALITY panel (§ Phase C1) — how COMPLETE GIGBAY's information is for a symbol across the 7
// intelligence domains, as a 0-100 score + readiness state (READY / PARTIAL / INSUFFICIENT). A missing
// source shows NO DATA (never fabricated). When the data is incomplete the panel says so plainly —
// "NOT READY FOR CAPITAL" — so a high conviction score is never mistaken for a high-quality one.
// Read-only; never a trade or order.
import React, { useEffect, useState } from "react";
import { fetchCompleteness } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { domainLabel, hasCompleteness, stateTone, type Completeness } from "@/lib/completeness";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}

export function DataQualityView({ data, loading, error }: { data: Completeness | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Measuring data completeness…" />;
  if (error) return <Box title={NO_DATA} note="Data completeness unavailable — nothing is shown, nothing invented." />;
  if (!hasCompleteness(data))
    return <Box title={NO_DATA} note="No completeness reading yet. It appears once the intelligence layers report in — never fabricated." />;

  const d = data as Completeness;
  const tone = stateTone(d.state);
  const ready = d.state === "READY";
  // Available first (most complete → least), then partial; missing rendered separately.
  const availOrdered = [...d.available, ...d.partial];

  return (
    <div className={`card consensus dqcard ${tone}`}>
      <div className="chead">
        <div>
          <div className="label">Data Completeness</div>
          <div className="csym">{d.symbol}</div>
        </div>
        <div className="cbig">
          <span className={`dqscore ${tone}`}>{d.score == null ? NO_DATA : d.score}<small>{d.score == null ? "" : " / 100"}</small></span>
          <span className={`dqstate ${tone}`}>{d.state}</span>
        </div>
      </div>

      <div className="dqbar-wrap"><i className={tone} style={{ width: `${Math.max(0, Math.min(100, d.score ?? 0))}%` }} /></div>

      {!ready ? (
        <div className={`dqwarn ${tone}`}>
          <b>{d.state === "INSUFFICIENT" ? "⛔ INSUFFICIENT DATA" : "⚠ PARTIAL ASSESSMENT"}</b>
          <span> — NOT READY FOR CAPITAL. A conviction score built on incomplete data is not a high-quality signal.</span>
        </div>
      ) : null}

      <div className="fsr">
        <div className="fcol">
          <div className="label">Available</div>
          {availOrdered.length
            ? availOrdered.map((k) => (
                <div className={`fitem ${d.available.includes(k) ? "pos" : "warn"}`} key={k}>
                  {d.available.includes(k) ? "✓" : "◐"} {domainLabel(d, k)}
                  <span className="dqpct"> {d.details[k]?.score ?? 0}%</span>
                </div>))
            : <div className="fitem neu">{NO_DATA}</div>}
        </div>
        <div className="fcol">
          <div className="label">Missing</div>
          {d.missing.length
            ? d.missing.map((k) => <div className="fitem neg" key={k}>⚠ {domainLabel(d, k)} <span className="dqpct">NO DATA</span></div>)
            : <div className="fitem neu">{NO_DATA}</div>}
        </div>
      </div>
    </div>
  );
}

export function DataQualityPanel({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: Completeness | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchCompleteness(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <DataQualityView data={state.data} loading={state.loading} error={state.error} />;
}
