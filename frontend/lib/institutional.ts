// Institutional Intelligence types (§ Phase R1.3). Mirrors /market/{symbol}/institutional-flow. "Smart
// money" = 13F quarter-over-quarter position changes + SEC Form 4 insider BUY/SELL. Read-only
// intelligence input — never a trade or copy-trade. Missing data → NO DATA, never fabricated.

export type ChangeDirection = "ACCUMULATION" | "REDUCTION" | "NEW_POSITION" | "EXIT";
export type InsiderSentiment = "BULLISH" | "NEUTRAL" | "BEARISH";

export interface InstitutionalChange {
  institution: string;
  symbol: string;
  previous_shares: number | null;
  current_shares: number | null;
  share_change: number | null;
  percentage_change: number | null;
  direction: ChangeDirection | null;
  filing_period: string | null;
}

export interface InsiderTx {
  insider_name: string | null;
  title: string | null;
  transaction_type: "BUY" | "SELL" | null;
  shares: number | null;
  price: number | null;
  transaction_date: string | null;
}

export type ClusterType = "ACCUMULATION" | "DISTRIBUTION" | "NONE";

export interface InsiderCluster {
  cluster_type: ClusterType | null;
  score: number | null;
  insider_count: number;
  summary: string | null;
}

export interface InstitutionalFlow {
  symbol: string;
  status: string | null;                 // COMPLETE / NO DATA
  institutional_changes: InstitutionalChange[];
  institutional_direction: string | null;  // ACCUMULATION / REDUCTION / MIXED
  accumulation_score: number | null;
  net_share_change_pct: number | null;
  insider_activity: InsiderTx[];
  insider_sentiment: InsiderSentiment | null;
  insider_score: number | null;
  insider_summary: { buy_count: number; sell_count: number; buy_shares: number; sell_shares: number; distinct_buyers: number };
  insider_cluster: InsiderCluster;       // § R1.4 cluster refinement
}

export function hasInstitutional(f: InstitutionalFlow | null | undefined): boolean {
  return !!f && f.status === "COMPLETE" && (f.institutional_changes.length > 0 || f.insider_activity.length > 0);
}

/** Tone for an institutional direction / accumulation. */
export function flowTone(d: string | null | undefined): "acc" | "red" | "mixed" | "neu" {
  return d === "ACCUMULATION" ? "acc" : d === "REDUCTION" || d === "EXIT" ? "red"
    : d === "MIXED" ? "mixed" : "neu";
}

/** Tone for insider sentiment. */
export function insiderTone(s: string | null | undefined): "acc" | "red" | "mixed" | "neu" {
  return s === "BULLISH" ? "acc" : s === "BEARISH" ? "red" : s === "NEUTRAL" ? "mixed" : "neu";
}

/** Tone for an insider cluster type. */
export function clusterTone(c: string | null | undefined): "acc" | "red" | "mixed" | "neu" {
  return c === "ACCUMULATION" ? "acc" : c === "DISTRIBUTION" ? "red" : c === "NONE" ? "mixed" : "neu";
}

/** Short cluster label, e.g. "ACCUMULATION" → "ACCUM". */
export function clusterLabel(c: string | null | undefined): string {
  return c === "ACCUMULATION" ? "ACCUMULATION" : c === "DISTRIBUTION" ? "DISTRIBUTION" : c === "NONE" ? "NONE" : "NO DATA";
}

/** Compact share count, e.g. 2,000,000 → "2.0M". */
export function fmtShares(n: number | null | undefined): string {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}
