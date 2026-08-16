// AI performance + history types (§ Phase G3.1). Mirrors the Control API's /ai/performance and
// /market/{symbol}/ai-history. Read-only evaluation of the AI's predictiveness — history is never
// rewritten and failed predictions are never removed. Missing outcomes → NO DATA; nothing fabricated.

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

/** Short date for a prediction timestamp, e.g. "Aug 16". */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { month: "short", day: "numeric" });
}
