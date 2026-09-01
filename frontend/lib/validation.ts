// § R3.1A — AI-validation research types + fetchers. RESEARCH DATA ONLY: no trading, no orders, no
// execution. Confidence is a heuristic score, never a probability; probabilistic calibration is NOT
// APPLICABLE. Missing/insufficient data renders as INSUFFICIENT DATA / NO DATA, never fabricated.
import { API_BASE } from "./api";

export interface ValidationCoverage {
  universe: { id: string; version: string; symbols: string[]; asset_class: string; exchange: string;
    calendar_version: string; currency: string };
  policies: Record<string, string>;
  coverage: {
    raw_snapshot_count: number; captured_snapshots: number; effective_canonical_sessions: number;
    unique_symbols: number; symbols: string[]; matured_total: number; graded_total: number;
    abstained_total: number; failed_total: number;
    by_horizon: Record<string, { matured: number; graded: number; abstained: number; failed: number;
      effective_graded_sessions: number }>;
  };
  raw_operational_prediction_count: number;
  confidence: { is_probability: boolean; note: string; probability_calibration: string };
  legacy_reconciliation: {
    governance_orphan_count: number; aggregate_mismatch: boolean;
    nvda_governance_count: number; nvda_prediction_count: number;
    per_symbol: { symbol: string; predictions: number; governance: number;
      governance_without_predictions: boolean }[];
  };
  gate: Record<string, unknown>;
  gate_id: string;
  safety: { research_only: boolean; autonomous: string; execution: string; ibkr_orders: number };
}

export interface ValidationRun {
  run_id: string; status: string; gate_passed?: boolean | null; gate_id: string; result_checksum: string | null;
  commit_sha: string | null; created_at: string | null; ended_at: string | null;
  validation_policy_version?: string; outcome_policy_version?: string;
  gate_report?: { passed: boolean; criteria: Record<string, { ok: boolean; actual: unknown; threshold: unknown }> };
  metrics?: Record<string, unknown>;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/${path}`, { signal, cache: "no-store",
    headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return res.json();
}

export const fetchValidationCoverage = (s?: AbortSignal) =>
  get<ValidationCoverage>("research-validation/coverage", s);
export const fetchValidationRuns = (s?: AbortSignal) =>
  get<{ count: number; runs: ValidationRun[] }>("research-validation/runs", s);
export const fetchValidationRun = (id: string, s?: AbortSignal) =>
  get<ValidationRun>(`research-validation/runs/${encodeURIComponent(id)}`, s);

// ---------------------------------------------------------------------------------------------------
// § R3.1A.2 — the validation GATE. A positive verdict anywhere in the UI (including the legacy AI
// Performance panel) is permitted ONLY when the LATEST COMPLETED validation run passed its preregistered
// gate. Everything else — no run, a RUNNING/INSUFFICIENT/FAILED latest run, a missing gate report, or an
// unreachable backend — is NOT VALIDATED. Fail closed: never optimistic, never fabricated.
export type GateReason = "VALIDATED" | "GATE_NOT_PASSED" | "NO_COMPLETED_RUN" | "UNAVAILABLE";

export interface ValidationGate {
  validated: boolean;
  reason: GateReason;
  run_id: string | null;
  status: string | null;          // status of the latest run of ANY status (what the operator sees)
  created_at: string | null;
}

/** The fail-closed default used whenever validation status cannot be read. */
export const NOT_VALIDATED: ValidationGate = {
  validated: false, reason: "UNAVAILABLE", run_id: null, status: null, created_at: null,
};

const byNewest = (a: ValidationRun, b: ValidationRun) =>
  String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""));

/** Pure: derive the gate from a runs page (any order — sorted newest-first here for determinism). */
export function validationGate(runs: ValidationRun[] | null | undefined): ValidationGate {
  if (!Array.isArray(runs) || runs.length === 0) return { ...NOT_VALIDATED, reason: "NO_COMPLETED_RUN" };
  const sorted = [...runs].sort(byNewest);
  const latest = sorted[0];
  const completed = sorted.find((r) => r.status === "COMPLETED");
  if (!completed)
    return { validated: false, reason: "NO_COMPLETED_RUN", run_id: latest.run_id,
      status: latest.status ?? null, created_at: latest.created_at ?? null };
  // `gate_passed` comes from the run summary; fall back to the detail's gate_report. Anything that is not
  // exactly `true` (null, undefined, missing report) counts as NOT passed.
  const passed = completed.gate_passed ?? completed.gate_report?.passed ?? null;
  return { validated: passed === true, reason: passed === true ? "VALIDATED" : "GATE_NOT_PASSED",
    run_id: completed.run_id, status: latest.status ?? null, created_at: completed.created_at ?? null };
}

/** Read the gate over the same-origin proxy. NEVER rejects — an unreachable backend is NOT VALIDATED. */
export async function fetchValidationGate(s?: AbortSignal): Promise<ValidationGate> {
  try {
    const page = await fetchValidationRuns(s);
    return validationGate(page?.runs);
  } catch {
    return NOT_VALIDATED;
  }
}

export function statusTone(s: string | null | undefined): "ready" | "warning" | "blocked" | "nodata" {
  return s === "COMPLETED" ? "ready" : s === "RUNNING" ? "warning"
    : s === "INSUFFICIENT" ? "nodata" : s === "FAILED" ? "blocked" : "nodata";
}
