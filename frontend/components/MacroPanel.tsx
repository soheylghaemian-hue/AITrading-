"use client";
// MACRO ENVIRONMENT panel (§ Phase R1.2) — the global macro backdrop as an intelligence input: a 0-100
// score, the risk regime (RISK ON / RISK NEUTRAL / RISK OFF), the driving signals + risks, and the raw
// metrics (rates, inflation, VIX, USD, commodities) with their trend. Read-only; never a trade. No
// snapshot / backend down → NO DATA (never fabricated).
import React, { useEffect, useState } from "react";
import { fetchMacro } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { hasMacro, regimeLabel, regimeTone, type Macro, type MacroMetric } from "@/lib/macro";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="card"><div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div></div>;
}

const TREND_MARK: Record<string, string> = { up: "▲", down: "▼", flat: "→" };
// Order the metrics for display.
const METRIC_ORDER = ["fed_rate", "treasury_10y", "treasury_2y", "cpi", "unemployment", "vix", "dxy", "oil", "gold"];

function fmt(m: MacroMetric): string {
  if (m.value == null) return NO_DATA;
  return `${m.value}${m.trend ? ` ${TREND_MARK[m.trend] ?? ""}` : ""}`;
}

export function MacroView({ data, loading, error }: { data: Macro | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Reading the macro environment…" />;
  if (error) return <Box title={NO_DATA} note="Macro environment unavailable — nothing is shown, nothing invented." />;
  if (!hasMacro(data))
    return <Box title={NO_DATA} note="No macro reading yet. The global regime appears once a macro data provider (e.g. FRED) is configured — never fabricated." />;

  const d = data as Macro;
  const tone = regimeTone(d.regime);
  const metrics = METRIC_ORDER.filter((k) => d.metrics[k]).map((k) => ({ key: k, ...d.metrics[k] }));

  return (
    <div className={`card consensus macrocard ${tone}`}>
      <div className="chead">
        <div>
          <div className="label">Macro Environment</div>
          <div className="csym">Global</div>
        </div>
        <div className="cbig">
          <span className={`macroscore ${tone}`}>{d.score == null ? NO_DATA : d.score}<small>{d.score == null ? "" : " / 100"}</small></span>
          <span className={`regimeb ${tone}`}>{regimeLabel(d.regime)}</span>
        </div>
      </div>

      <div className="macrometrics">
        {metrics.map((m) => (
          <div className="macrometric" key={m.key}>
            <span className="mlabel">{m.label}</span>
            <span className={`mval num t-${m.trend ?? "none"}`}>{fmt(m)}</span>
          </div>
        ))}
      </div>

      <div className="fsr">
        <div className="fcol">
          <div className="label">Drivers</div>
          {d.signals.length ? d.signals.map((s) => <div className="fitem pos" key={s}>✓ {s}</div>)
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

export function MacroPanel() {
  const [state, setState] = useState<{ data: Macro | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    fetchMacro(ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, []);
  return <MacroView data={state.data} loading={state.loading} error={state.error} />;
}
