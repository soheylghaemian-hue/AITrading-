// Backtesting research types + helpers (§ Phase R3.0). Mirrors the Control API's read-only /backtests
// endpoints. RESEARCH ONLY — a backtest is an internal historical research run; it never trades, never
// places or submits an order, never enables execution. Missing/insufficient data renders as NO DATA /
// INSUFFICIENT, never a fabricated number.

export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";

export interface BacktestRun {
  run_id: string;
  status: RunStatus | string | null;
  strategy_id: string | null;
  strategy_version: number | null;
  engine_version?: string | null;
  interval: string | null;
  symbols: string[];
  start: string | null;
  end: string | null;
  asset_class?: string | null;
  result_checksum: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  created_at: string | null;
  ended_at: string | null;
  // detail-only
  timestamp_policy_id?: string | null;
  exchange_tz?: string | null;
  strategy_config?: Record<string, unknown> | null;
  config_snapshot?: Record<string, unknown> | null;
  risk_config_snapshot?: Record<string, unknown> | null;
  warnings?: string[];
  missing_data?: { symbols?: CoverageSymbol[] } | null;
  safety?: { research_only?: boolean; autonomous?: string; execution?: string; ibkr_orders?: number };
}

export interface CoverageSymbol {
  symbol: string;
  expected_bars: number;
  available_bars: number;
  missing_bars: number;
  missing_ratio: number;
  usable_bars: number;
  ok: boolean;
  reason: string | null;
}

export interface BacktestMetrics {
  [k: string]: unknown;
  status?: string;
  metrics?: Record<string, unknown> | null;
}

export interface BacktestTrade {
  id: string; symbol: string; side: string; entry_ts: string; entry_price: number | null;
  initial_stop_price: number | null; exit_ts: string | null; exit_price: number | null;
  quantity: number | null; net_pnl: number | null; commission: number | null; slippage: number | null;
  return_pct: number | null; bars_held: number | null; exit_reason: string | null; ambiguous: boolean;
}

export interface EquityPoint {
  seq: number; ts: string; cash: number | null; equity: number | null; realized_pnl: number | null;
  unrealized_pnl: number | null; drawdown_pct: number | null;
}

export interface BacktestEvent {
  id: string; seq: number | null; ts: string | null; event_type: string; severity: string | null;
  symbol: string | null; details: Record<string, unknown>;
}

export function statusTone(s: string | null | undefined): "ready" | "warning" | "blocked" | "nodata" {
  return s === "COMPLETED" ? "ready" : s === "RUNNING" || s === "QUEUED" ? "warning"
    : s === "FAILED" || s === "CANCELLED" ? "blocked" : "nodata";
}

/** A metric value from the API is a number, "NO DATA", "NOT APPLICABLE", or a nested object. */
export function metricText(v: unknown, digits = 2, suffix = ""): string {
  if (v == null) return "NO DATA";
  if (typeof v === "string") return v;                       // "NO DATA" / "NOT APPLICABLE" / "inf"
  if (typeof v === "number" && Number.isFinite(v)) return v.toFixed(digits) + suffix;
  return "NO DATA";
}

export function pctText(v: unknown, digits = 2): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return metricText(v);
  return (v * 100).toFixed(digits) + "%";
}
