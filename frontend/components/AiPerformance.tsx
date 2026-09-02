"use client";
// AI PERFORMANCE (§ Phase G3.1, gated in § R3.1A.2) — honest evaluation of the AI's predictiveness:
// directional accuracy, average forward return, confidence calibration, best/weakest inputs, error
// breakdown. Read-only. Loading → spinner; no evaluated predictions → NO DATA (never fabricated metrics).
//
// These are LEGACY metrics: the legacy hourly operational prediction history, NOT the R3.1A canonical
// one-sample-per-symbol-per-session validation set. Every metric is therefore labelled LEGACY, and NO
// positive verdict (calibration or overall) is rendered unless the latest COMPLETED validation run passed
// its preregistered gate. Otherwise the panel states NOT VALIDATED / INSUFFICIENT DATA. Fail closed: an
// unreachable validation backend counts as NOT VALIDATED.
import React, { useEffect, useState } from "react";
import { fetchPerformance } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { gateNote, gatedAccTone, gatedVerdict, hasPerformance, INSUFFICIENT_LABEL, LEGACY,
  NOT_VALIDATED_LABEL, type AiPerformance } from "@/lib/performance";
import { fetchValidationGate, NOT_VALIDATED, type ValidationGate } from "@/lib/validation";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}
function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="fmetric">
      <div className="label">{label} <span className="vd-ins-tag">{LEGACY}</span></div>
      <div className={`v num ${tone || ""}`}>{value}</div>
    </div>
  );
}

/** The verdict banner. Positive language is reachable only through a passed validation gate. */
function GateBanner({ gate }: { gate: ValidationGate }) {
  if (gate.validated)
    return (
      <div className="fanalyst">
        <div className="label">Overall Verdict</div>
        <div className="fameta">
          <span>Validation <b>VALIDATED</b></span>
          <span>Run <b>{gate.run_id || NO_DATA}</b></span>
          <span>{gateNote(gate.reason)}</span>
        </div>
      </div>
    );
  return (
    <div className="fanalyst vd-insufficient">
      <div className="label">Overall Verdict</div>
      <div className="vd-ins-tag">{NOT_VALIDATED_LABEL} · {INSUFFICIENT_LABEL}</div>
      <p>
        {gateNote(gate.reason)} These are {LEGACY} operational metrics, not the canonical validation
        sample — no performance verdict is claimed.
      </p>
    </div>
  );
}

export function PerformanceView({ data, loading, error, gate = NOT_VALIDATED }:
  { data: AiPerformance | null; loading: boolean; error: string | null; gate?: ValidationGate | null }) {
  if (loading) return <Box title="LOADING" note="Evaluating AI performance…" />;
  if (error) return <Box title={NO_DATA} note="AI performance unavailable — nothing is shown, nothing invented." />;
  if (!hasPerformance(data))
    return <Box title={`${NO_DATA} · ${NOT_VALIDATED_LABEL}`} note={`Not enough evaluated ${LEGACY} predictions yet — ${INSUFFICIENT_LABEL}. Accuracy appears once AI predictions have matured against real price outcomes — never fabricated.`} />;

  const g = gate ?? NOT_VALIDATED;                       // null/undefined gate ⇒ fail closed
  const validated = g.validated === true;
  const d = data as AiPerformance;
  const cal = d.confidence_calibration;
  const pctv = (x: number | null) => (x == null ? NO_DATA : `${x}%`);
  const ret = d.average_return;
  // A positive average return is a positive verdict too — its "up" tone is withheld until validation.
  const retTone = ret == null ? "" : ret >= 0 ? (validated ? "up" : "") : "down";
  return (
    <div className="card consensus">
      <div className="chead">
        <div>
          <div className="label">AI Performance <span className="vd-ins-tag">{LEGACY} METRICS</span></div>
          <div className="csym" style={{ fontSize: 16 }}>Last {d.sample_size} evaluated · {d.horizon_days}-day horizon</div>
        </div>
      </div>
      <GateBanner gate={g} />
      <div className="fgrid" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
        <Stat label="Directional Accuracy" value={pctv(d.direction_accuracy)} tone={gatedAccTone(d.direction_accuracy, validated)} />
        <Stat label="Average Return" value={ret == null ? NO_DATA : `${ret >= 0 ? "+" : ""}${ret}%`} tone={retTone} />
        <Stat label="Bullish Accuracy" value={pctv(d.bullish_accuracy)} tone={gatedAccTone(d.bullish_accuracy, validated)} />
        <Stat label="Bearish Accuracy" value={pctv(d.bearish_accuracy)} tone={gatedAccTone(d.bearish_accuracy, validated)} />
      </div>

      {d.by_horizon ? (
        <div className="fgrid" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
          {[1, 3, 5, 20].map((h) => {
            const m = d.by_horizon![String(h)];
            const a = m ? m.accuracy : null;
            return <Stat key={h} label={`${h}-Day Accuracy`}
              value={a == null ? NO_DATA : `${a}%`} tone={gatedAccTone(a, validated)} />;
          })}
        </div>
      ) : null}

      <div className="fanalyst">
        <div className="label">Confidence Calibration <span className="vd-ins-tag">{LEGACY}</span></div>
        <div className="fameta">
          <span>Verdict <b>{gatedVerdict(cal, validated)}</b></span>
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
  const [state, setState] = useState<{ data: AiPerformance | null; gate: ValidationGate; loading: boolean; error: string | null }>({
    data: null, gate: NOT_VALIDATED, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    // The validation status is fetched alongside the metrics; `fetchValidationGate` never rejects, so a
    // validation outage degrades to NOT VALIDATED instead of silently showing an ungated verdict.
    Promise.all([fetchPerformance(5, ctrl.signal), fetchValidationGate(ctrl.signal)])
      .then(([r, gate]) => { if (!cancelled) setState({ data: r, gate, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, gate: NOT_VALIDATED, loading: false,
          error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, []);
  return <PerformanceView data={state.data} loading={state.loading} error={state.error} gate={state.gate} />;
}
