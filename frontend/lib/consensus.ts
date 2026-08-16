// AI consensus types (§ Phase G3). Mirrors the Control API's /market/{symbol}/ai-consensus. The AI
// market view is an intelligence signal only — never a trading decision. Every number traces to a real
// intelligence layer; missing inputs → NO DATA / PARTIAL. Disagreements are surfaced, never hidden.

export type Direction = "BULLISH" | "NEUTRAL" | "BEARISH";

export interface ConsensusComponent {
  component_name: string;
  score: number | null;
  weight: number;
  direction: string;                 // bullish / bearish / neutral
  reason: string;
  risk_flags: string[];
}

export interface AiConsensus {
  symbol: string;
  score: number | null;
  direction: Direction | null;
  confidence: number | null;
  status: string | null;             // COMPLETE / PARTIAL / NO DATA
  coverage: number;
  components: ConsensusComponent[];
  strengths: string[];
  risks: string[];
  conflicts: string[];
}

/** True only when a real conviction score exists (else NO DATA). */
export function hasConsensus(c: AiConsensus | null | undefined): boolean {
  return !!c && c.score != null;
}

export function directionTone(d: string | null | undefined): "pos" | "neg" | "neu" {
  return d === "BULLISH" || d === "bullish" ? "pos" : d === "BEARISH" || d === "bearish" ? "neg" : "neu";
}

export function scoreTier(q: number | null | undefined): "hi" | "med" | "lo" {
  if (q == null) return "lo";
  return q >= 70 ? "hi" : q >= 45 ? "med" : "lo";
}
