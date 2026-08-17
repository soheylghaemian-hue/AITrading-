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
  run_id: string; status: string; gate_id: string; result_checksum: string | null;
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

export function statusTone(s: string | null | undefined): "ready" | "warning" | "blocked" | "nodata" {
  return s === "COMPLETED" ? "ready" : s === "RUNNING" ? "warning"
    : s === "INSUFFICIENT" ? "nodata" : s === "FAILED" ? "blocked" : "nodata";
}
