"use client";
// § R3.1A — AI Validation research view. RESEARCH DATA ONLY — forward-only immutable point-in-time
// intelligence collection + deterministic prediction-quality validation. It NEVER trades, places an order,
// enables execution, or generalizes beyond the pilot. Confidence is a heuristic score (probability
// calibration NOT APPLICABLE). Insufficient evidence renders INSUFFICIENT DATA, never a fabricated result.
import React, { useEffect, useState } from "react";
import { NO_DATA } from "@/lib/format";
import {
  fetchValidationCoverage, fetchValidationRuns, type ValidationCoverage, type ValidationRun, statusTone,
} from "@/lib/validation";

function SafetyBanner() {
  return (
    <div className="bt-banner">
      <span className="bt-b-tag">RESEARCH DATA ONLY</span>
      <span className="bt-b-tag">PILOT · US EQUITY (AAPL/NVDA/SPY)</span>
      <span className="bt-b-pill">AUTONOMOUS <b>DISABLED</b></span>
      <span className="bt-b-pill">EXECUTION <b>DISABLED</b></span>
      <span className="bt-b-pill">IBKR ORDERS <b>0</b></span>
    </div>
  );
}

const HZ = ["1", "3", "5", "20"];

export function Validation({ connected }: { connected: boolean }) {
  const [cov, setCov] = useState<ValidationCoverage | null>(null);
  const [runs, setRuns] = useState<ValidationRun[]>([]);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let live = true;
    Promise.all([fetchValidationCoverage().catch(() => null), fetchValidationRuns().catch(() => null)])
      .then(([c, r]) => { if (!live) return; setCov(c); setRuns(r?.runs || []); if (!c) setErr(true); });
    return () => { live = false; };
  }, []);

  const latest = runs[0];
  const insufficient = !latest || latest.status === "INSUFFICIENT";

  return (
    <>
      {!connected ? <div className="banner"><span className="dot r" aria-hidden="true" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. Nothing is fabricated.</div> : null}
      <SafetyBanner />

      <div className={`bt-status ${insufficient ? "nodata" : statusTone(latest?.status)}`}>
        <div><div className="label">Universe</div><div>{cov?.universe.id ?? NO_DATA}</div></div>
        <div><div className="label">Effective sessions</div><div className="num">{cov?.coverage.effective_canonical_sessions ?? NO_DATA}</div></div>
        <div><div className="label">Matured outcomes</div><div className="num">{cov?.coverage.matured_total ?? NO_DATA}</div></div>
        <div><div className="label">Raw operational (legacy)</div><div className="num">{cov?.raw_operational_prediction_count ?? NO_DATA}</div></div>
        <div><div className="label">Latest run</div><div className={`bt-state ${insufficient ? "nodata" : statusTone(latest?.status)}`}>{latest?.status ?? "NONE"}</div></div>
      </div>

      {insufficient ? (
        <div className="card vd-insufficient">
          <div className="vd-ins-tag">INSUFFICIENT DATA</div>
          <p>The preregistered evidence gate <b>{cov?.gate_id ?? "VALIDATION_GATE_US_EQUITY_PILOT_V1"}</b> is
            not met, so no prediction-quality result is produced. Collection is forward-only and immutable;
            nothing is backfilled or fabricated. Probability calibration is <b>NOT APPLICABLE</b> (confidence
            is a heuristic score, not a probability).</p>
        </div>
      ) : null}

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>Collection coverage by horizon (graded predictions vs abstentions)</h3>
        {cov ? (
          <table className="bt-cov"><thead><tr><th>Horizon (sessions)</th><th>Matured</th><th>Graded</th><th>Abstained</th><th>Effective sessions</th><th>Failed</th></tr></thead>
            <tbody>{HZ.map((h) => {
              const b = cov.coverage.by_horizon[h] || { matured: 0, graded: 0, abstained: 0, effective_graded_sessions: 0, failed: 0 };
              return <tr key={h}><td>{h}</td><td className="num">{b.matured}</td><td className="num">{b.graded}</td><td className="num">{b.abstained}</td><td className="num">{b.effective_graded_sessions}</td><td className="num">{b.failed}</td></tr>;
            })}</tbody></table>
        ) : <div className="nodata"><div className="nd">{NO_DATA}</div></div>}
      </div>

      <div className="grid k2">
        <div className="card"><h3 style={{ marginBottom: 8 }}>Confidence semantics</h3>
          <div className="vd-note">
            <div className="ds-m"><span className="label">Confidence is a probability?</span><b>{cov ? String(cov.confidence.is_probability) : NO_DATA}</b></div>
            <div className="ds-m"><span className="label">Probability calibration</span><b>{cov?.confidence.probability_calibration ?? "NOT APPLICABLE"}</b></div>
            <p className="bt-disclaimer">{cov?.confidence.note ?? "heuristic 0-100 score"} — Brier / log-loss / ECE are NOT APPLICABLE until a separately versioned probability contract exists.</p>
          </div>
        </div>

        <div className="card"><h3 style={{ marginBottom: 8 }}>Legacy reconciliation</h3>
          {cov ? (
            <div className="vd-note">
              <div className="ds-m"><span className="label">Governance rows without a persisted prediction</span><b className={`num ${cov.legacy_reconciliation.governance_orphan_count ? "down" : ""}`}>{cov.legacy_reconciliation.governance_orphan_count}</b></div>
              <div className="ds-m"><span className="label">Aggregate/per-symbol mismatch</span><b>{String(cov.legacy_reconciliation.aggregate_mismatch)}</b></div>
              <div className="ds-m"><span className="label">NVDA governance vs predictions</span><b className="num">{cov.legacy_reconciliation.nvda_governance_count} / {cov.legacy_reconciliation.nvda_prediction_count}</b></div>
              <p className="bt-disclaimer">Read-only diagnostic — legacy rows are never modified. New R3.1A rows enforce a snapshot foreign key so a decision/outcome without a persisted snapshot is impossible.</p>
            </div>
          ) : <div className="nodata"><div className="nd">{err ? NO_DATA : "…"}</div></div>}
        </div>
      </div>

      {latest?.gate_report ? (
        <div className="card"><h3 style={{ marginBottom: 8 }}>Evidence gate ({latest.gate_id})</h3>
          <table className="bt-cov"><thead><tr><th>Criterion</th><th>Met</th><th>Actual</th><th>Threshold</th></tr></thead>
            <tbody>{Object.entries(latest.gate_report.criteria).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td>{v.ok ? "✓" : "✗"}</td><td className="num">{JSON.stringify(v.actual).slice(0, 40)}</td><td className="num">{JSON.stringify(v.threshold)}</td></tr>
            ))}</tbody></table>
          <p className="bt-disclaimer">Passing this gate permits only a pilot validation study — it does not authorize trading or generalize beyond AAPL/NVDA/SPY.</p>
        </div>
      ) : null}
    </>
  );
}
