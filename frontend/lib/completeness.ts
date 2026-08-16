// Data Completeness types (§ Phase C1). Mirrors the Control API's /market/{symbol}/data-completeness.
// A read-only reliability layer: how complete GIGBAY's information is across the 7 intelligence domains.
// A missing source scores 0 (NO DATA) — never fabricated, and the score never rises to cover a gap.

export type CompletenessState = "READY" | "PARTIAL" | "INSUFFICIENT";

export interface CompletenessDomain {
  label: string;
  weight: number;
  score: number;                       // 0-100 for this domain
  available: boolean;
  checks: Record<string, boolean>;
}

export interface Completeness {
  symbol: string;
  score: number | null;                // 0-100 overall
  state: CompletenessState | null;
  available: string[];
  missing: string[];
  partial: string[];
  details: Record<string, CompletenessDomain>;
}

export function hasCompleteness(c: Completeness | null | undefined): boolean {
  return !!c && c.score != null && !!c.state;
}

/** Tone class suffix for a readiness state — drives the badge/border colour. */
export function stateTone(s: string | null | undefined): "ready" | "partial" | "insufficient" | "neu" {
  return s === "READY" ? "ready" : s === "PARTIAL" ? "partial"
    : s === "INSUFFICIENT" ? "insufficient" : "neu";
}

/** Human label for a domain key, from the API details (falls back to a title-cased key). */
export function domainLabel(c: Completeness, key: string): string {
  return c.details?.[key]?.label ?? key.charAt(0).toUpperCase() + key.slice(1);
}

/** Whether the data is complete enough to carry high-confidence weight. */
export function readyForCapital(c: Completeness | null | undefined): boolean {
  return !!c && c.state === "READY";
}
