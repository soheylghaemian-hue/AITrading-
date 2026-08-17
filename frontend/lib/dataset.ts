// Research OHLC dataset types + helpers (§ Phase R3.0A). Mirrors the Control API's /research/datasets
// endpoints. RESEARCH DATA ONLY — an immutable, versioned, checksum-verified dataset built from
// split-adjusted 1-minute aggregates normalized to regular-session (RTH) daily bars. It never trades,
// never places or submits an order, never enables execution, and never touches live ohlc_bars.
// Missing/insufficient coverage renders as NO DATA / MISSING, never a fabricated number.

export type DatasetStatus = "PLANNED" | "RUNNING" | "COMPLETED" | "FAILED" | string;

export interface DatasetEvent {
  seq: number | null; ts: string | null; event_type: string; severity: string | null;
  symbol: string | null; details: Record<string, unknown>;
}

export interface ResearchDataset {
  dataset_id: string;
  owner?: string | null;
  status: DatasetStatus;
  symbols: string[];
  interval: string | null;
  provider: string | null;
  provider_contract_version?: string | null;
  adjustment_policy: string | null;
  normalization_policy?: string | null;
  calendar_version: string | null;
  range_start: string | null;
  range_end: string | null;
  row_count: number | null;
  dataset_checksum: string | null;
  raw_pages_checksum?: string | null;
  request_checksum?: string | null;
  provider_adjusted_flag?: boolean | null;
  supersedes_dataset_id?: string | null;
  retry_of_dataset_id?: string | null;
  superseded_by?: string[];
  failure_code?: string | null;
  failure_reason?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  // detail-only
  missing_minute_threshold?: string | null;
  warnings?: string[];
  missing_data?: Record<string, unknown> | null;
  events?: DatasetEvent[];
}

export interface DatasetCoverageSymbol {
  symbol: string; bar_count: number; first_ts: string | null; last_ts: string | null;
}

export interface DatasetCoverage {
  dataset_id: string; status: DatasetStatus; interval: string | null;
  range_start: string | null; range_end: string | null; adjustment_policy: string | null;
  dataset_checksum: string | null; per_symbol: DatasetCoverageSymbol[];
  missing_data: Record<string, unknown> | null;
}

export function datasetTone(s: DatasetStatus | null | undefined): "ready" | "warning" | "blocked" | "nodata" {
  return s === "COMPLETED" ? "ready" : s === "RUNNING" || s === "PLANNED" ? "warning"
    : s === "FAILED" ? "blocked" : "nodata";
}

/** Short checksum for compact display (keeps the sha256: prefix legible). */
export function shortChecksum(c: string | null | undefined, n = 12): string {
  if (!c) return "NO DATA";
  const body = c.startsWith("sha256:") ? c.slice(7) : c;
  return "sha256:" + body.slice(0, n);
}

export function rangeText(d: Pick<ResearchDataset, "range_start" | "range_end">): string {
  if (!d.range_start || !d.range_end) return "NO DATA";
  return `${d.range_start} → ${d.range_end}`;
}
