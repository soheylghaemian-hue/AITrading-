"use client";
// Operator Capital-Readiness (§ UX-1) for the Overview page. Composes the GLOBAL capital-deployment
// gates from real read-model fields — risk configuration, risk-data availability, broker/portfolio-data
// availability and execution state (data-completeness & governance are per-symbol → shown as NO DATA
// here). READY is never inferred from the absence of positions. Read-only: never a trade control.
import React, { useEffect, useState } from "react";
import type { Snapshot } from "@/lib/types";
import { fetchRiskStatus, fetchRiskConfig } from "@/lib/api";
import type { RiskStatus, RiskConfigView } from "@/lib/risk";
import { computeReadiness } from "@/lib/readiness";
import { CapitalReadiness } from "./CapitalReadiness";

export function OverviewReadiness({ snapshot, connected }: { snapshot: Snapshot | null; connected: boolean }) {
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [config, setConfig] = useState<RiskConfigView | null>(null);
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    fetchRiskStatus(ctrl.signal).then((r) => { if (!cancelled) setRisk(r); }).catch((e: any) => { if (!cancelled && e?.name !== "AbortError") setRisk(null); });
    fetchRiskConfig(ctrl.signal).then((r) => { if (!cancelled) setConfig(r); }).catch((e: any) => { if (!cancelled && e?.name !== "AbortError") setConfig(null); });
    const id = setInterval(() => {
      fetchRiskStatus(ctrl.signal).then((r) => { if (!cancelled) setRisk(r); }).catch(() => {});
    }, 15000);
    return () => { cancelled = true; ctrl.abort(); clearInterval(id); };
  }, []);
  const readiness = computeReadiness({ snapshot, connected, riskStatus: risk, riskConfig: config });
  return <CapitalReadiness readiness={readiness} />;
}
