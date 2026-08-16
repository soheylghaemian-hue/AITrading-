// Trader intelligence types (§ Phase G2.5). Mirrors the Control API's /market/{symbol}/traders. This
// is an intelligence signal (quality-weighted consensus), NOT copy-trading and NOT execution. Every
// value traces to real persisted trader data or is NO DATA — nothing is fabricated.

export type Consensus = "BULLISH" | "BEARISH" | "NEUTRAL";

export interface TraderContributor {
  id: string;
  name: string;
  quality: number | null;
  strategy: string | null;
  market_focus: string | null;
  direction: string;
}

export interface TraderConsensus {
  symbol: string;
  consensus: Consensus | null;
  long_percent: number | null;
  short_percent: number | null;
  neutral_percent: number | null;
  weighted_score: number | null;
  contributor_count: number;
  contributors: TraderContributor[];
}

/** True only when real trader coverage exists for the symbol (else the tab shows NO DATA). */
export function hasTraderData(t: TraderConsensus | null | undefined): boolean {
  return !!t && (t.contributor_count > 0 || (Array.isArray(t.contributors) && t.contributors.length > 0));
}

export function consensusTone(c: string | null | undefined): "pos" | "neg" | "neu" {
  return c === "BULLISH" ? "pos" : c === "BEARISH" ? "neg" : "neu";
}

export function directionTone(d: string | null | undefined): "pos" | "neg" | "neu" {
  const u = (d || "").toUpperCase();
  return u === "LONG" ? "pos" : u === "SHORT" ? "neg" : "neu";
}

/** Quality tier for badge coloring. Null quality → low. */
export function qualityTier(q: number | null | undefined): "hi" | "med" | "lo" {
  if (q == null) return "lo";
  return q >= 80 ? "hi" : q >= 50 ? "med" : "lo";
}
