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

export async function setRiskConfig(
  cfg: { capital: number; risk_per_trade_pct: number; max_daily_loss_pct: number },
  token = "",
): Promise<{ ok: boolean; detail: string; data?: any }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured — connect a read-only backend to apply config" };
  const res = await fetch(`${API_BASE}/dashboard/risk-config`, {
    method: "POST", headers: mutHeaders(token, true), body: JSON.stringify(cfg),
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, detail: body.detail || (res.ok ? "updated" : `${res.status}`), data: body };
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
