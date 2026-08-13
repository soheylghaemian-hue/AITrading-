// Mirrors the backend Dashboard API snapshot (atp.dashboard.snapshot). The backend is the
// single source of truth; every field here is optional because the public frontend must render
// gracefully (NO DATA) when the backend is unreachable — it never fabricates values.

export interface MarketDataRow {
  symbol: string;
  asset_class?: string;
  exchange?: string | null;
  status: "DATA_AVAILABLE" | "DELAYED" | "STALE" | "DATA_NOT_AVAILABLE" | "ERROR";
  market_data_type?: string | null;
  bid?: number | null;
  ask?: number | null;
  last?: number | null;
  timestamp?: string | null;
  reason?: string | null;
  error_code?: number | null;
}

export interface AiAnalysisRow {
  agent: string;
  instrument: string;
  status: "SIGNAL" | "OBSERVATION" | "NO DATA";
  action?: string | null;
  confidence?: number | null;
  expected_return?: number | null;
  reason?: string | null;
}

export interface Snapshot {
  generated_at?: string;
  mode?: string;
  system_status?: string;
  connected?: boolean | null;
  execution_enabled?: boolean;
  orders?: number;
  account?: Record<string, number | null>;
  risk?: Record<string, number | boolean | string | null>;
  positions?: Record<string, any>[];
  market?: Record<string, any>;
  market_data?: MarketDataRow[];
  subscriptions?: Record<string, any>[];
  ai_analysis?: AiAnalysisRow[];
  tradeable_universe?: {
    symbol: string; asset_class?: string; exchange?: string | null; tradeable: boolean;
    state: "TRADEABLE" | "BLOCKED"; data_type?: string | null; last_valid_timestamp?: string | null;
    ibkr_error?: number | null; reason: string;
  }[];
  trading_risk?: {
    capital: number; risk_per_trade_pct: number; max_risk_per_trade: number;
    max_daily_loss_pct: number; max_daily_loss: number; current_daily_pnl: number;
    remaining_daily_risk: number; status: "ACTIVE" | "DAILY LOSS LIMIT REACHED";
  } | null;
  autonomous?: {
    mode: string; status: "DISABLED" | "ARMED" | "RUNNING" | "HALTED" | "KILLED";
    paper_equity: number | null; today_pnl: number | null; open_positions: number | null;
    trades_today: number; risk_used: number | null; remaining_daily_loss: number | null;
    max_daily_loss: number | null; live_execution: boolean; ibkr_orders: number;
    engine?: string; data?: string; risk?: string; dry_run?: boolean; dry_run_until?: string | null;
    metrics?: {
      total_evaluations: number; opportunities_detected: number; potential_trades: number;
      approved_decisions: number; rejected_decisions: number; no_data_decisions: number;
      risk_vetoes: number; avg_confidence: number | null; avg_expected_risk: number | null;
      avg_suggested_position: number | null; signals_by_instrument: Record<string, number>;
      signals_by_agent: Record<string, number>;
    };
    audit?: { actor: string; ts: string; prev: string; new: string; reason: string }[];
    decisions: { ts: string; instrument: string; agent?: string | null; action: string | null;
      signal_strength?: number | null; confidence?: number | null; expected_risk?: number | null;
      suggested_size?: number | null; approved_size?: number | null; entry?: number | null;
      stop?: number | null; target?: number | null; risk_decision?: string | null;
      quantity?: number | null; price?: number | null; execution_decision?: string; decision?: string;
      reason: string }[];
  } | null;
  agents?: Record<string, any>[];
  governance?: Record<string, any>[];
  system_health?: Record<string, string>;
  hero?: Record<string, number | null>;
  analytics_overall?: Record<string, number | null>;
  recent_trades?: Record<string, any>[];
  notifications?: Record<string, any>[];
  n_trades?: number;
}

export interface SnapshotState {
  data: Snapshot | null;
  loading: boolean;
  // true only when a live backend answered; false => NO DATA (unreachable / not configured).
  connected: boolean;
  error: string | null;
  lastFetch: string | null;
}
