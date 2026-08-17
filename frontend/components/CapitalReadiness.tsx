"use client";
// Capital Readiness (§ Phase UX-1) — PURE. Renders the composed readiness verdict (READY / NOT READY /
// NO DATA) and the per-input checklist: Data Completeness, AI Governance, Risk Configuration, Risk-Data
// Availability, Broker/Portfolio-Data Availability, Execution State. READY is never inferred from the
// absence of positions; every check reflects a real read-model field. Read-only — never a trade control.
import React from "react";
import { type Readiness, readinessTone, checkTone } from "@/lib/readiness";

const DOT: Record<string, string> = { ready: "g", warning: "o", blocked: "r", nodata: "grey" };

export function CapitalReadiness({ readiness }: { readiness: Readiness }) {
  const tone = readinessTone(readiness.label);
  return (
    <div className={`card readiness ${tone}`}>
      <div className="rd-head">
        <div>
          <div className="label">Capital Readiness</div>
          <div className={`rd-verdict ${tone}`}>{readiness.label}</div>
        </div>
        <p className="rd-disclaimer">Readiness reflects real inputs only. It is never inferred from the absence of positions, and it never enables trading.</p>
      </div>
      <ul className="rd-checks">
        {readiness.checks.map((c) => (
          <li className={`rd-check ${checkTone(c.state)}`} key={c.key}>
            <span className={`dot ${DOT[checkTone(c.state)]}`} aria-hidden="true" />
            <span className="rd-label">{c.label}</span>
            <span className="rd-detail">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Compact one-line readiness pill for headers/summary rows. */
export function ReadinessPill({ readiness }: { readiness: Readiness }) {
  const tone = readinessTone(readiness.label);
  return (
    <span className={`rd-pill ${tone}`}>
      <span className={`dot ${DOT[tone]}`} aria-hidden="true" />
      Capital <b>{readiness.label}</b>
    </span>
  );
}
