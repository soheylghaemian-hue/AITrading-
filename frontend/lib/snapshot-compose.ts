// Compose the frontend Snapshot from the ATP Control / Observability API.
//
// That backend has NO /dashboard/summary — its read routes are /status, /broker and /market. The
// server proxy fetches those three and maps their REAL fields into the Snapshot shape here. Anything
// the observability API does not provide (risk numbers, AI decisions, per-position detail) is left
// undefined so the UI renders NO DATA. Nothing is fabricated: every value traces to a backend field.
import type { Snapshot } from "./types";

type Raw = Record<string, any> | null | undefined;

function numOrNull(v: any): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function mapEngine(runtime?: string): "DISABLED" | "ARMED" | "RUNNING" | "HALTED" | "KILLED" {
  const r = (runtime || "").toUpperCase();
  if (r === "RUNNING" || r === "STARTED") return "RUNNING";
  if (r === "ARMED") return "ARMED";
  if (r === "HALTED" || r === "PAUSED") return "HALTED";
  if (r === "KILLED") return "KILLED";
  return "DISABLED";
}

export function composeSnapshot(status: Raw, broker: Raw, market: Raw): Snapshot {
  const S = status || {};
  const B = broker || {};
  const M = market || {};

  // System health: one row per backend service, DB from /status.db, and the broker LINK state.
  const services: any[] = Array.isArray(S.services) ? S.services : [];
  const system_health: Record<string, string> = {};
  for (const svc of services) {
    if (svc && typeof svc.service === "string" && typeof svc.status === "string") {
      system_health[svc.service] = svc.status;
    }
  }
  if (typeof S.db === "boolean") system_health.database = S.db ? "UP" : "DOWN";
  if (typeof B.connection === "string") system_health.broker = B.connection; // CONNECTED/DISCONNECTED/STALE

  // Market rows (Markets view + symbolQuote). Control API covers US symbols (AAPL/NVDA/SPY).
  const mrows: any[] = Array.isArray(M.market_data) ? M.market_data
    : Array.isArray(S.market_data) ? S.market_data : [];
  const global_market_data = mrows.map((r: any) => {
    const bid = numOrNull(r.bid), ask = numOrNull(r.ask);
    return {
      region: "USA",
      exchange: null as string | null,
      symbol: String(r.symbol ?? ""),
      source: r.source ?? null,
      status: String(r.status ?? "DATA_NOT_AVAILABLE"),
      realtime: !!r.realtime,
      bid, ask, last: numOrNull(r.last),
      spread: bid != null && ask != null ? ask - bid : null,
      bid_size: numOrNull(r.bid_size), ask_size: numOrNull(r.ask_size),
      volume: numOrNull(r.volume),
      latency_ms: numOrNull(r.latency_ms),
      timestamp: r.last_update ?? r.timestamp ?? null,
      error: r.error ?? null,
      subscription_state: String(r.status ?? ""),
    };
  });
  const market_data = mrows.map((r: any) => ({
    symbol: String(r.symbol ?? ""),
    status: String(r.status ?? "DATA_NOT_AVAILABLE") as any,
    market_data_type: r.realtime ? "REALTIME" : null,
    bid: numOrNull(r.bid), ask: numOrNull(r.ask), last: numOrNull(r.last),
    timestamp: r.last_update ?? r.timestamp ?? null,
    reason: r.error ?? null,
  }));

  const runtime = typeof S.runtime_state === "string" ? S.runtime_state
    : typeof B.runtime_state === "string" ? B.runtime_state : undefined;

  const autonomous = {
    mode: String(B.mode ?? runtime ?? "PAPER"),
    status: mapEngine(runtime),
    paper_equity: numOrNull(B.equity),
    today_pnl: null,
    open_positions: numOrNull(B.position_count),
    trades_today: 0,
    risk_used: null,
    remaining_daily_loss: null,
    max_daily_loss: null,
    live_execution: B.execution_enabled === true,
    ibkr_orders: 0,
    decisions: [] as any[],
  };

  return {
    generated_at: S.ts ?? M.ts ?? B.ts ?? new Date().toISOString(),
    mode: B.mode ?? undefined,
    system_status: runtime,
    // Snapshot.connected is the BROKER link (see statusStrip); page-level "backend answered" is separate.
    connected: typeof B.connection === "string" ? B.connection.toUpperCase() === "CONNECTED" : null,
    execution_enabled: B.execution_enabled === true,
    orders: numOrNull(B.open_order_count) ?? 0,
    n_trades: 0,
    account: {
      equity: numOrNull(B.equity),
      cash: numOrNull(B.cash),
      buying_power: numOrNull(B.buying_power),
    },
    positions: [],
    market_data,
    global_market_data,
    system_health,
    autonomous,
    trading_risk: null, // observability API exposes no risk budget numbers → Risk Center shows NO DATA
  };
}
