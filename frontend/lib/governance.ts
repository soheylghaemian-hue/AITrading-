// AI Decision Governance types (§ Phase G3.3). Mirrors the Control API's /market/{symbol}/ai-governance
// and /ai/governance. This layer evaluates decision QUALITY and READINESS only — it never executes
// trades, generates orders, or touches the broker/IBKR. Missing inputs → BLOCKED, never fabricated.

export type GovernanceStatus = "APPROVED" | "PARTIAL" | "CONFLICT" | "BLOCKED";

export interface Governance {
  symbol: string;
  status: GovernanceStatus | null;
  score: number | null;
  confidence: number | null;
  data_completeness: number | null;
  reasons: string[];
  approved: boolean;
  direction: string | null;
  missing: string[];
  conflicts: string[];
}

export interface GovernanceDecision {
  prediction_id: string;
  symbol: string;
  status: GovernanceStatus | null;
  score: number | null;
  confidence: number | null;
  data_completeness: number | null;
  reasons: string[];
  approved: boolean;
  direction: string | null;
  timestamp: string | null;
  outcome: { time_horizon: number; return_percentage: number | null; direction_correct: boolean | null; status?: string | null } | null;
}

export interface GovernanceFeed {
  count: number;
  decisions: GovernanceDecision[];
  status_counts: Record<string, number>;
}

const VALID: GovernanceStatus[] = ["APPROVED", "PARTIAL", "CONFLICT", "BLOCKED"];

export function hasGovernance(g: Governance | null | undefined): boolean {
  return !!g && !!g.status && (VALID as string[]).includes(g.status);
}

/** Tone class suffix for a governance status — drives the badge/border colour. */
export function govTone(s: string | null | undefined): "approved" | "partial" | "conflict" | "blocked" | "neu" {
  return s === "APPROVED" ? "approved" : s === "PARTIAL" ? "partial"
    : s === "CONFLICT" ? "conflict" : s === "BLOCKED" ? "blocked" : "neu";
}

// Deterministic reason-code → human text. The codes are the source of truth; this is display only.
const REASON_TEXT: Record<string, string> = {
  MISSING_OPTIONS: "Options data missing",
  MISSING_FUNDAMENTALS: "Fundamentals missing",
  MISSING_NEWS: "News missing",
  LOW_CONFIDENCE: "Confidence below threshold",
  LOW_SCORE: "Conviction below threshold",
  LOW_COMPLETENESS: "Incomplete intelligence coverage",
  SOURCE_CONFLICT: "Sources disagree",
  RISK_BLOCK: "Severe risk condition",
  INSUFFICIENT_DATA: "Insufficient data",
  INCOMPLETE: "Assessment incomplete",
};

export function reasonText(code: string): string {
  return REASON_TEXT[code] ?? code.replace(/_/g, " ").toLowerCase();
}
