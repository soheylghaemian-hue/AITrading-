// AI performance + history types (§ Phase G3.1). Mirrors the Control API's /ai/performance and
// /market/{symbol}/ai-history. Read-only evaluation of the AI's predictiveness — history is never
// rewritten and failed predictions are never removed. Missing outcomes → NO DATA; nothing fabricated.
import { NO_DATA } from "./format";

export interface CalibrationBucket { count: number; success_rate: number | null; avg_confidence: number | null }
export interface Calibration { high: CalibrationBucket; medium: CalibrationBucket; low: CalibrationBucket; verdict: string | null }
export interface HorizonMetric { accuracy: number | null; average_return: number | null; sample_size: number }

export interface AiPerformance {
  sample_size: number;
  overall_accuracy: number | null;
  direction_accuracy: number | null;
  bullish_accuracy: number | null;
  bearish_accuracy: number | null;
  average_return: number | null;
  confidence_calibration: Calibration | null;
  score_reliability: { high_score_accuracy: number | null; low_score_accuracy: number | null } | null;
  horizon_days: number;
  errors: Record<string, number>;
  best_inputs: string[];
  weakest_inputs: string[];
  by_horizon?: Record<string, HorizonMetric>;      // §G3.2: 1/3/5/20-day accuracy + avg return
}

// §G3.2 Outcome Lifecycle status.
export interface AiOutcomes {
  prediction_count: number;
  evaluated_count: number;
  pending_count: number;
  accuracy: number | null;
  horizons: number[];
  classification: Record<string, number>;
}

export interface AiHistoryOutcome {
  time_horizon: number;
  prediction_price?: number | null;
  future_price: number | null;
  return_percentage: number | null;
  direction_expected?: string | null;
  direction_actual?: string | null;
  direction_correct: boolean | null;
  result?: string | null;
  status?: string | null;
}
export interface AiHistoryGovernance {          // §G3.3: governance verdict stored with the prediction
  status: string | null; score: number | null; confidence: number | null;
  data_completeness: number | null; reasons: string[]; approved: boolean;
}
export interface AiHistoryItem {
  id: string; symbol: string; timestamp: string | null; score: number | null;
  direction: string | null; confidence: number | null; status: string | null;
  price_at_prediction: number | null; outcomes: AiHistoryOutcome[];
  governance?: AiHistoryGovernance;
}
export interface AiHistory { symbol: string; count: number; assessments: AiHistoryItem[] }

export function hasPerformance(p: AiPerformance | null | undefined): boolean {
  return !!p && p.sample_size > 0;
}
export function hasHistory(h: AiHistory | null | undefined): boolean {
  return !!h && Array.isArray(h.assessments) && h.assessments.length > 0;
}
export function directionTone(d: string | null | undefined): "pos" | "neg" | "neu" {
  return d === "BULLISH" ? "pos" : d === "BEARISH" ? "neg" : "neu";
}
export function accTone(a: number | null | undefined): "pos" | "neg" | "neu" {
  if (a == null) return "neu";
  return a >= 60 ? "pos" : a < 45 ? "neg" : "neu";
}

// ---------------------------------------------------------------------------------------------------
// § R3.1A.2 — LEGACY gating. These metrics come from the legacy hourly operational prediction history,
// NOT from the R3.1A canonical one-sample-per-symbol-per-session validation set, so they are labelled
// LEGACY everywhere and may NEVER carry a positive verdict unless the latest COMPLETED validation run
// passed its preregistered gate. Fail closed: unknown validation status ⇒ NOT VALIDATED.
export const LEGACY = "LEGACY";
export const NOT_VALIDATED_LABEL = "NOT VALIDATED";
export const INSUFFICIENT_LABEL = "INSUFFICIENT DATA";

/** The calibration verdict actually allowed on screen. Ungated verdicts (e.g. "Good") are suppressed. */
export function gatedVerdict(cal: Calibration | null | undefined, validated: boolean): string {
  if (!validated) return NOT_VALIDATED_LABEL;
  return cal?.verdict || NO_DATA;
}

/** Accuracy tone, with the POSITIVE tone withheld until validation passes. Negative stays honest. */
export function gatedAccTone(a: number | null | undefined, validated: boolean): "pos" | "neg" | "neu" {
  const tone = accTone(a);
  return validated ? tone : tone === "neg" ? "neg" : "neu";
}

/** One-line explanation of why the legacy panel carries no verdict. */
export function gateNote(reason: string | null | undefined): string {
  switch (reason) {
    case "VALIDATED": return "Latest COMPLETED validation run passed its preregistered gate.";
    case "GATE_NOT_PASSED": return "The latest COMPLETED validation run did NOT pass its preregistered gate.";
    case "NO_COMPLETED_RUN": return "No COMPLETED validation run exists yet.";
    default: return "Validation status is unavailable — treated as not validated.";
  }
}

/** Short date for a prediction timestamp, e.g. "Aug 16". */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { month: "short", day: "numeric" });
}
