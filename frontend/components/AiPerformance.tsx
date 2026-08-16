"use client";
// AI PERFORMANCE (§ Phase G3.1) — honest evaluation of the AI's predictiveness: directional accuracy,
// average forward return, confidence calibration, best/weakest inputs, error breakdown. Read-only.
// Loading → spinner; no evaluated predictions → NO DATA (never fabricated metrics).
import React, { useEffect, useState } from "react";
import { fetchPerformance } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { accTone, hasPerformance, type AiPerformance } from "@/lib/performance";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}
function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return <div className="fmetric"><div className="label">{label}</div><div className={`v num ${tone || ""}`}>{value}</div></div>;
}

export function PerformanceView({ data, loading, error }: { data: AiPerformance | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Evaluating AI performance…" />;
  if (error) return <Box title={NO_DATA} note="AI performance unavailable — nothing is shown, nothing invented." />;
  if (!hasPerformance(data))
    return <Box title={NO_DATA} note="Not enough evaluated predictions yet. Accuracy appears once AI predictions have matured against real price outcomes — never fabricated." />;

  const d = data as AiPerformance;
  const cal = d.confidence_calibration;
  const pctv = (x: number | null) => (x == null ? NO_DATA : `${x}%`);
  const ret = d.average_return;
  return (
    <div className="card consensus">
      <div className="chead">
        <div>
          <div className="label">AI Performance</div>
          <div className="csym" style={{ fontSize: 16 }}>Last {d.sample_size} evaluated · {d.horizon_days}-day horizon</div>
        </div>
      </div>
      <div className="fgrid" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
        <Stat label="Directional Accuracy" value={pctv(d.direction_accuracy)} tone={accTone(d.direction_accuracy)} />
        <Stat label="Average Return" value={ret == null ? NO_DATA : `${ret >= 0 ? "+" : ""}${ret}%`} tone={ret == null ? "" : ret >= 0 ? "up" : "down"} />
        <Stat label="Bullish Accuracy" value={pctv(d.bullish_accuracy)} tone={accTone(d.bullish_accuracy)} />
        <Stat label="Bearish Accuracy" value={pctv(d.bearish_accuracy)} tone={accTone(d.bearish_accuracy)} />
      </div>

      <div className="fanalyst">
        <div className="label">Confidence Calibration</div>
        <div className="fameta">
          <span>Verdict <b>{cal?.verdict || NO_DATA}</b></span>
          <span>High <b>{cal ? `${cal.high.success_rate == null ? NO_DATA : cal.high.success_rate + "%"} (${cal.high.count})` : NO_DATA}</b></span>
          <span>Medium <b>{cal ? `${cal.medium.success_rate == null ? NO_DATA : cal.medium.success_rate + "%"} (${cal.medium.count})` : NO_DATA}</b></span>
          <span>Low <b>{cal ? `${cal.low.success_rate == null ? NO_DATA : cal.low.success_rate + "%"} (${cal.low.count})` : NO_DATA}</b></span>
        </div>
      </div>

      <div className="fsr">
        <div className="fcol">
          <div className="label">Best Performing Inputs</div>
          {d.best_inputs.length ? d.best_inputs.map((s) => <div className="fitem pos" key={s}>✓ {s}</div>)
            : <div className="fitem neu">{NO_DATA}</div>}
        </div>
        <div className="fcol">
          <div className="label">Weakest Inputs / Errors</div>
          {d.weakest_inputs.length ? d.weakest_inputs.map((s) => <div className="fitem neg" key={s}>⚠ {s}</div>) : null}
          {Object.entries(d.errors).map(([k, v]) => <div className="fitem neg" key={k}>⚠ {k}: {v}</div>)}
          {!d.weakest_inputs.length && !Object.keys(d.errors).length ? <div className="fitem neu">{NO_DATA}</div> : null}
        </div>
      </div>
    </div>
  );
}

export function AiPerformancePanel() {
  const [state, setState] = useState<{ data: AiPerformance | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    fetchPerformance(5, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, []);
  return <PerformanceView data={state.data} loading={state.loading} error={state.error} />;
}
