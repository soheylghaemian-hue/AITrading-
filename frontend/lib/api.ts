// Read-only client for the backend Dashboard API. The public frontend ONLY ever performs:
//   - GET  <API_BASE>/dashboard/summary            (read snapshot)
//   - POST <API_BASE>/dashboard/emergency-stop     (owner-token, backend enforces the kill switch)
//   - POST <API_BASE>/dashboard/resume
// It NEVER connects to IB Gateway, never places/cancels orders, never holds broker credentials.
// The emergency stop is authoritative in the BACKEND RiskEngine; the browser only sends a
// token-authenticated request — it cannot touch the broker itself.

import type { Snapshot } from "./types";
import type { OhlcBar } from "./ohlc";
import type { NewsItem } from "./news";
import type { TraderConsensus } from "./traders";
import type { FundamentalsData } from "./fundamentals";
import type { OptionsData } from "./options";
import type { AiConsensus } from "./consensus";
import type { AiPerformance, AiHistory, AiOutcomes } from "./performance";
import type { Governance, GovernanceFeed } from "./governance";
import type { Completeness } from "./completeness";
import type { Macro, MacroContext } from "./macro";
import type { InstitutionalFlow, InsiderCluster } from "./institutional";
import type { RiskStatus, RiskConfigView, RiskEvents } from "./risk";
import type { BacktestRun, BacktestMetrics, BacktestTrade, EquityPoint, BacktestEvent } from "./backtest";

// Where the browser sends dashboard calls. Default: the SAME-ORIGIN server proxy ("/api"), which
// forwards to the private backend and injects the read token server-side — so no token ever
// reaches the browser (spec §Auth). NEXT_PUBLIC_API_URL may override it for local dev (direct
// backend). It must only ever be an API address — never a secret.
export const API_BASE = (
  process.env.NEXT_PUBLIC_API_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api"
).replace(/\/+$/, "");

// Reserved for a future backend push channel (WebSocket/SSE). The backend is REST-only today,
// so the dashboard uses polling; when a WS endpoint exists, set NEXT_PUBLIC_WS_URL.
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "";

// Configurable poll interval (ms). Defaults to 4000; clamped to a sane floor.
export const POLL_MS = Math.max(1000, Number(process.env.NEXT_PUBLIC_POLL_MS ?? "4000") || 4000);

export async function fetchSnapshot(signal?: AbortSignal): Promise<Snapshot> {
  if (!API_BASE) {
    // No backend configured (e.g. public Vercel deploy) → surface NO DATA, never fabricate.
    throw new Error("NO_BACKEND");
  }
  const res = await fetch(`${API_BASE}/dashboard/summary`, {
    signal,
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return (await res.json()) as Snapshot;
}

export interface OhlcResponse {
  symbol: string;
  interval: string;
  bars: OhlcBar[];
}

// Durable OHLC candles for the Market Intelligence Terminal. Read-only, via the SAME same-origin server
// proxy (no token in the browser); the proxy forwards to the Control API's /market/{symbol}/ohlc. On no
// backend / non-OK, it throws — the caller renders NO DATA (never fabricates candles).
export async function fetchOhlc(
  symbol: string, interval: string, limit = 500, signal?: AbortSignal,
): Promise<OhlcResponse> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(
    `${API_BASE}/dashboard/ohlc/${encodeURIComponent(symbol)}?interval=${encodeURIComponent(interval)}&limit=${limit}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } },
  );
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const body = (await res.json()) as Partial<OhlcResponse>;
  return {
    symbol: body.symbol ?? symbol,
    interval: body.interval ?? interval,
    bars: Array.isArray(body.bars) ? body.bars : [],
  };
}

export interface NewsResponse {
  symbol: string;
  items: NewsItem[];
}

// Market news for the terminal News tab (§ Phase G2.1). Read-only, via the SAME same-origin server
// proxy (no token in the browser); the proxy forwards to the Control API's /market/{symbol}/news. On
// no backend / non-OK it throws — the caller renders NO DATA (never fabricates a headline).
export async function fetchNews(
  symbol: string, limit = 30, signal?: AbortSignal,
): Promise<NewsResponse> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(
    `${API_BASE}/dashboard/news/${encodeURIComponent(symbol)}?limit=${limit}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } },
  );
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const body = (await res.json()) as Partial<NewsResponse>;
  return { symbol: body.symbol ?? symbol, items: Array.isArray(body.items) ? body.items : [] };
}

// Trader-intelligence consensus for the terminal Traders tab (§ Phase G2.5). Read-only, via the SAME
// same-origin server proxy; the proxy forwards to the Control API's /market/{symbol}/traders. On no
// backend / non-OK it throws — the caller renders NO DATA (never fabricates a trader or consensus).
export async function fetchTraders(symbol: string, signal?: AbortSignal): Promise<TraderConsensus> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/traders/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<TraderConsensus>;
  return {
    symbol: b.symbol ?? symbol,
    consensus: b.consensus ?? null,
    long_percent: b.long_percent ?? null,
    short_percent: b.short_percent ?? null,
    neutral_percent: b.neutral_percent ?? null,
    weighted_score: b.weighted_score ?? null,
    contributor_count: b.contributor_count ?? 0,
    contributors: Array.isArray(b.contributors) ? b.contributors : [],
  };
}

// Single-trader profile (performance / risk / strategy). Proxy forwards to the Control API /traders/{id}.
export async function fetchTrader(id: string, signal?: AbortSignal): Promise<any> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/trader/${encodeURIComponent(id)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return res.json();
}

// Fundamentals intelligence for the terminal Fundamentals tab (§ Phase G2.2). Read-only, via the SAME
// same-origin server proxy; the proxy forwards to the Control API's /market/{symbol}/fundamentals. On no
// backend / non-OK it throws — the caller renders NO DATA (never fabricates a financial value).
export async function fetchFundamentals(symbol: string, signal?: AbortSignal): Promise<FundamentalsData> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/fundamentals/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<FundamentalsData>;
  return {
    symbol: b.symbol ?? symbol,
    company: b.company ?? null,
    quality_score: b.quality_score ?? null,
    quality_breakdown: b.quality_breakdown ?? null,
    financials: b.financials ?? null,
    valuation: b.valuation ?? null,
    analyst_estimates: b.analyst_estimates ?? null,
    strengths: Array.isArray(b.strengths) ? b.strengths : [],
    risks: Array.isArray(b.risks) ? b.risks : [],
  };
}

// Options intelligence for the terminal Options tab (§ Phase G2.3). Read-only, via the SAME same-origin
// server proxy; the proxy forwards to the Control API's /market/{symbol}/options. On no backend / non-OK
// it throws — the caller renders NO DATA (never fabricates IV / flow / unusual activity).
export async function fetchOptions(symbol: string, signal?: AbortSignal): Promise<OptionsData> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/options/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<OptionsData>;
  return {
    symbol: b.symbol ?? symbol,
    options_score: b.options_score ?? null,
    call_put_ratio: b.call_put_ratio ?? null,
    implied_volatility: b.implied_volatility ?? null,
    volume: b.volume ?? null,
    call_volume: b.call_volume ?? null,
    put_volume: b.put_volume ?? null,
    open_interest: b.open_interest ?? null,
    premium_volume: b.premium_volume ?? null,
    unusual_activity: b.unusual_activity ?? null,
    unusual_activity_score: b.unusual_activity_score ?? null,
    large_trade_count: b.large_trade_count ?? null,
    sentiment: b.sentiment ?? null,
    signals: Array.isArray(b.signals) ? b.signals : [],
    risks: Array.isArray(b.risks) ? b.risks : [],
  };
}

// AI consensus for the AI Brain page + terminal summary (§ Phase G3). Read-only, via the SAME
// same-origin server proxy; the proxy forwards to the Control API's /market/{symbol}/ai-consensus. On
// no backend / non-OK it throws — the caller renders NO DATA (never fabricates a conviction).
export async function fetchConsensus(symbol: string, signal?: AbortSignal): Promise<AiConsensus> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-consensus/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<AiConsensus>;
  return {
    symbol: b.symbol ?? symbol,
    score: b.score ?? null,
    direction: b.direction ?? null,
    confidence: b.confidence ?? null,
    status: b.status ?? null,
    coverage: b.coverage ?? 0,
    components: Array.isArray(b.components) ? b.components : [],
    strengths: Array.isArray(b.strengths) ? b.strengths : [],
    risks: Array.isArray(b.risks) ? b.risks : [],
    conflicts: Array.isArray(b.conflicts) ? b.conflicts : [],
  };
}

// AI performance metrics (§ Phase G3.1). Read-only, via the same-origin proxy → Control API /ai/performance.
export async function fetchPerformance(horizon = 5, signal?: AbortSignal): Promise<AiPerformance> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-performance?horizon=${horizon}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<AiPerformance>;
  return {
    sample_size: b.sample_size ?? 0,
    overall_accuracy: b.overall_accuracy ?? null,
    direction_accuracy: b.direction_accuracy ?? null,
    bullish_accuracy: b.bullish_accuracy ?? null,
    bearish_accuracy: b.bearish_accuracy ?? null,
    average_return: b.average_return ?? null,
    confidence_calibration: b.confidence_calibration ?? null,
    score_reliability: b.score_reliability ?? null,
    horizon_days: b.horizon_days ?? horizon,
    errors: b.errors ?? {},
    best_inputs: Array.isArray(b.best_inputs) ? b.best_inputs : [],
    weakest_inputs: Array.isArray(b.weakest_inputs) ? b.weakest_inputs : [],
    by_horizon: b.by_horizon,                        // §G3.2: 1/3/5/20-day accuracy grid
  };
}

// Outcome Lifecycle status (§ Phase G3.2) → Control API /ai/outcomes.
export async function fetchOutcomes(signal?: AbortSignal): Promise<AiOutcomes> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-outcomes`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<AiOutcomes>;
  return {
    prediction_count: b.prediction_count ?? 0, evaluated_count: b.evaluated_count ?? 0,
    pending_count: b.pending_count ?? 0, accuracy: b.accuracy ?? null,
    horizons: Array.isArray(b.horizons) ? b.horizons : [1, 3, 5, 20],
    classification: b.classification ?? {},
  };
}

// AI prediction history for a symbol (§ Phase G3.1) → Control API /market/{symbol}/ai-history.
export async function fetchAiHistory(symbol: string, signal?: AbortSignal): Promise<AiHistory> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-history/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<AiHistory>;
  return { symbol: b.symbol ?? symbol, count: b.count ?? 0, assessments: Array.isArray(b.assessments) ? b.assessments : [] };
}

// § G3.3 — the current governance verdict for a symbol (APPROVED / PARTIAL / CONFLICT / BLOCKED).
export async function fetchGovernance(symbol: string, signal?: AbortSignal): Promise<Governance> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-governance/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<Governance>;
  return {
    symbol: b.symbol ?? symbol, status: b.status ?? null, score: b.score ?? null,
    confidence: b.confidence ?? null, data_completeness: b.data_completeness ?? null,
    reasons: Array.isArray(b.reasons) ? b.reasons : [], approved: !!b.approved,
    direction: b.direction ?? null, missing: Array.isArray(b.missing) ? b.missing : [],
    conflicts: Array.isArray(b.conflicts) ? b.conflicts : [],
  };
}

// § G3.3 — recent governance decisions (Prediction → Governance → Outcome).
export async function fetchGovernanceFeed(signal?: AbortSignal): Promise<GovernanceFeed> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/ai-governance`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<GovernanceFeed>;
  return {
    count: b.count ?? 0, decisions: Array.isArray(b.decisions) ? b.decisions : [],
    status_counts: b.status_counts ?? {},
  };
}

// § C1 — data completeness across the 7 intelligence domains (reliability / capital-protection layer).
export async function fetchCompleteness(symbol: string, signal?: AbortSignal): Promise<Completeness> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/data-completeness/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<Completeness>;
  return {
    symbol: b.symbol ?? symbol, score: b.score ?? null, state: b.state ?? null,
    available: Array.isArray(b.available) ? b.available : [],
    missing: Array.isArray(b.missing) ? b.missing : [],
    partial: Array.isArray(b.partial) ? b.partial : [],
    details: b.details ?? {},
  };
}

// § R1.2 — the current global macro environment (score, regime, signals/risks, metrics).
export async function fetchMacro(signal?: AbortSignal): Promise<Macro> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/macro`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<Macro>;
  return {
    score: b.score ?? null, regime: b.regime ?? null, status: b.status ?? null,
    signals: Array.isArray(b.signals) ? b.signals : [], risks: Array.isArray(b.risks) ? b.risks : [],
    metrics: b.metrics ?? {}, timestamp: b.timestamp ?? null, source: b.source ?? null,
  };
}

// § R1.3 — institutional "smart money" flow: 13F position changes + Form 4 insider activity.
export async function fetchInstitutionalFlow(symbol: string, signal?: AbortSignal): Promise<InstitutionalFlow> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/institutional-flow/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<InstitutionalFlow>;
  return {
    symbol: b.symbol ?? symbol, status: b.status ?? null,
    institutional_changes: Array.isArray(b.institutional_changes) ? b.institutional_changes : [],
    institutional_direction: b.institutional_direction ?? null,
    accumulation_score: b.accumulation_score ?? null, net_share_change_pct: b.net_share_change_pct ?? null,
    insider_activity: Array.isArray(b.insider_activity) ? b.insider_activity : [],
    insider_sentiment: b.insider_sentiment ?? null, insider_score: b.insider_score ?? null,
    insider_summary: b.insider_summary ?? { buy_count: 0, sell_count: 0, buy_shares: 0, sell_shares: 0, distinct_buyers: 0 },
    insider_cluster: b.insider_cluster ?? { cluster_type: null, score: null, insider_count: 0, summary: null },
  };
}

// § R1.4 — the insider cluster (ACCUMULATION / DISTRIBUTION / NONE) for a symbol.
export async function fetchInsiderCluster(symbol: string, signal?: AbortSignal): Promise<InsiderCluster & { symbol: string; status: string | null }> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/insider-cluster/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as any;
  return {
    symbol: b.symbol ?? symbol, status: b.status ?? null, cluster_type: b.cluster_type ?? null,
    score: b.score ?? null, insider_count: b.insider_count ?? 0, summary: b.summary ?? null,
  };
}

// § R1.2 — what the current macro regime means for a symbol (tailwind / neutral / headwind).
export async function fetchMacroContext(symbol: string, signal?: AbortSignal): Promise<MacroContext> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/macro-context/${encodeURIComponent(symbol)}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<MacroContext>;
  return {
    symbol: b.symbol ?? symbol, regime: b.regime ?? null, score: b.score ?? null,
    relevance: b.relevance ?? null, note: b.note ?? null, status: b.status ?? null,
    signals: Array.isArray(b.signals) ? b.signals : [], risks: Array.isArray(b.risks) ? b.risks : [],
  };
}

// Mutations are authorized by the SERVER proxy (it injects the owner token from a server env var),
// so the browser no longer prompts for a token. `token` stays optional for local/dev direct calls.
function mutHeaders(token = "", json = false): Record<string, string> {
  const h: Record<string, string> = {};
  if (token) h["Authorization"] = `Bearer ${token}`;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

export async function emergencyStop(token = ""): Promise<{ ok: boolean; detail: string }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/emergency-stop`, { method: "POST", headers: mutHeaders(token) });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, detail: body.reason || body.detail || `${res.status}` };
}

// § R2.0 — the read-only Risk Control state (READY/WARNING/BLOCKED/NO DATA + budget usage + limits +
// kill switch). Same-origin server proxy → Control API /risk/status. On no backend / non-OK it throws
// (the caller renders NO DATA — never a fabricated zero, never a false READY).
export async function fetchRiskStatus(signal?: AbortSignal): Promise<RiskStatus> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/risk-status`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<RiskStatus>;
  return {
    status: b.status ?? null, reasons: Array.isArray(b.reasons) ? b.reasons : [],
    missing: Array.isArray(b.missing) ? b.missing : [],
    capital: b.capital ?? { value: null, currency: null, source: null },
    daily_pnl: b.daily_pnl ?? { value: null, limit: null, used_pct: null, remaining: null, observed_at: null },
    position_risk: b.position_risk ?? { value: null, limit: null },
    exposure: b.exposure ?? { gross_pct: null, net_pct: null, limit_pct: null },
    drawdown: b.drawdown ?? { value_pct: null, limit_pct: null },
    kill_switch: b.kill_switch ?? null, configuration_version: b.configuration_version ?? null,
    version_token: b.version_token ?? null, updated_at: b.updated_at ?? null, ts: b.ts ?? null,
  };
}

// § R2.0 — the read-only current Risk Control configuration (canonical limits + the DERIVED
// max_daily_loss_amount + the concurrency version token). Proxy → Control API /risk/config (GET).
export async function fetchRiskConfig(signal?: AbortSignal): Promise<RiskConfigView> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/risk-config`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<RiskConfigView>;
  return {
    configured: !!b.configured, reason: b.reason ?? null,
    configuration_version: b.configuration_version ?? null, version_token: b.version_token ?? null,
    kill_switch: b.kill_switch ?? null, config: b.config ?? null,
  };
}

// § R2.0 — the immutable risk-event history (CONFIGURATION_UPDATED + kill-switch audit). Proxy →
// Control API /risk/events. Read-only; on no backend / non-OK it throws (caller shows NO DATA).
export async function fetchRiskEvents(limit = 50, signal?: AbortSignal): Promise<RiskEvents> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/risk-events?limit=${limit}`,
    { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = (await res.json()) as Partial<RiskEvents>;
  return { count: b.count ?? 0, events: Array.isArray(b.events) ? b.events : [] };
}

// § R2.0 — update the Risk Control configuration. Authorized by the SERVER proxy (owner token injected
// server-side, never in the browser); `expected_version` is the version_token from fetchRiskConfig
// (optimistic concurrency — a stale or out-of-band change is rejected 409). This changes LIMITS only:
// it writes the canonical risk_config + policy + an immutable audit event. It does NOT place an order,
// touch the broker/IBKR/execution, or enable trading, and it never mutates the kill switch.
export interface RiskConfigUpdate {
  expected_version?: string | null;
  capital: number;
  currency: string;
  max_daily_loss_pct: number;
  max_position_risk_pct: number;
  max_portfolio_exposure_pct: number;
  max_drawdown_pct: number;
  warning_threshold_pct: number;
}
export async function updateRiskConfig(
  cfg: RiskConfigUpdate, token = "",
): Promise<{ ok: boolean; detail: string; conflict?: boolean; errors?: string[]; data?: any }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured — connect a read-only backend to apply config" };
  const res = await fetch(`${API_BASE}/dashboard/risk-config`, {
    method: "POST", headers: mutHeaders(token, true), body: JSON.stringify(cfg),
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) return { ok: true, detail: "updated", data: body };
  // 422 → validation errors; 409 → version conflict (reload + retry); 401 → auth.
  const errors: string[] | undefined = Array.isArray(body?.detail?.errors) ? body.detail.errors : undefined;
  const detail = errors ? errors.join("; ")
    : (typeof body?.detail === "string" ? body.detail
      : body?.detail?.detail || (res.status === 401 ? "not authorized" : `${res.status}`));
  return { ok: false, detail, conflict: res.status === 409, errors, data: body };
}

export async function autonomousControl(
  action: "arm" | "disarm" | "dry_run" | "start" | "stop" | "kill" | "reset",
  payload: Record<string, unknown> = {}, token = "",
): Promise<{ ok: boolean; detail: string; data?: any }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/autonomous/${action}`, {
    method: "POST", headers: mutHeaders(token, true), body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  const detail = body.status || body.detail || (body.reasons ? body.reasons.join("; ") : `${res.status}`);
  return { ok: res.ok && body.ok !== false, detail, data: body };
}

export async function resumeTrading(token = ""): Promise<{ ok: boolean; detail: string }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/resume`, { method: "POST", headers: mutHeaders(token) });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, detail: body.reason || body.detail || `${res.status}` };
}

// § R3.0 — Backtesting research (read-only over the same-origin proxy; POST is authorized server-side).
// Creating a backtest starts an INTERNAL historical research run — it never enables trading, never
// creates a broker order. On no backend / non-OK the caller renders NO DATA (never fabricates results).
export interface BacktestCreateReq {
  strategy_id?: string; strategy_version?: number; symbols: string[]; interval: string;
  start: string; end: string; starting_capital?: string; currency?: string;
  costs?: Record<string, string>; risk?: Record<string, string>; max_concurrent_positions?: number;
}

export async function createBacktest(
  req: BacktestCreateReq, token = "",
): Promise<{ ok: boolean; detail: string; errors?: string[]; conflict?: boolean; data?: BacktestRun }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/backtests`, {
    method: "POST", headers: mutHeaders(token, true), body: JSON.stringify(req),
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) return { ok: true, detail: "created", data: body as BacktestRun };
  const errors: string[] | undefined = Array.isArray(body?.detail?.errors) ? body.detail.errors : undefined;
  const detail = errors ? errors.join("; ")
    : (typeof body?.detail === "string" ? body.detail
      : body?.detail?.detail || body?.detail?.message || (res.status === 401 ? "not authorized" : `${res.status}`));
  return { ok: false, detail, errors, conflict: res.status === 409, data: body };
}

export async function fetchBacktests(signal?: AbortSignal): Promise<{ count: number; runs: BacktestRun[] }> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = await res.json();
  return { count: b.count ?? 0, runs: Array.isArray(b.runs) ? b.runs : [] };
}

export async function fetchBacktest(runId: string, signal?: AbortSignal): Promise<BacktestRun> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests/${encodeURIComponent(runId)}`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return res.json();
}

export async function fetchBacktestMetrics(runId: string, signal?: AbortSignal): Promise<BacktestMetrics> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests/${encodeURIComponent(runId)}/metrics`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return res.json();
}

export async function fetchBacktestTrades(runId: string, signal?: AbortSignal): Promise<BacktestTrade[]> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests/${encodeURIComponent(runId)}/trades`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = await res.json();
  return Array.isArray(b.trades) ? b.trades : [];
}

export async function fetchBacktestEquity(runId: string, signal?: AbortSignal): Promise<EquityPoint[]> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests/${encodeURIComponent(runId)}/equity`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = await res.json();
  return Array.isArray(b.equity) ? b.equity : [];
}

export async function fetchBacktestEvents(runId: string, signal?: AbortSignal): Promise<BacktestEvent[]> {
  if (!API_BASE) throw new Error("NO_BACKEND");
  const res = await fetch(`${API_BASE}/dashboard/backtests/${encodeURIComponent(runId)}/events`, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const b = await res.json();
  return Array.isArray(b.events) ? b.events : [];
}
