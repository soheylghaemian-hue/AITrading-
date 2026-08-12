// Read-only client for the backend Dashboard API. The public frontend ONLY ever performs:
//   - GET  <API_BASE>/dashboard/summary            (read snapshot)
//   - POST <API_BASE>/dashboard/emergency-stop     (owner-token, backend enforces the kill switch)
//   - POST <API_BASE>/dashboard/resume
// It NEVER connects to IB Gateway, never places/cancels orders, never holds broker credentials.
// The emergency stop is authoritative in the BACKEND RiskEngine; the browser only sends a
// token-authenticated request — it cannot touch the broker itself.

import type { Snapshot } from "./types";

// Public base URL of the read-only backend Dashboard API. NEXT_PUBLIC_API_URL is the canonical
// name (spec §19); NEXT_PUBLIC_API_BASE_URL kept as a backward-compatible fallback.
export const API_BASE = (
  process.env.NEXT_PUBLIC_API_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? ""
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

export async function emergencyStop(token: string): Promise<{ ok: boolean; detail: string }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/emergency-stop`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, detail: body.reason || body.detail || `${res.status}` };
}

export async function resumeTrading(token: string): Promise<{ ok: boolean; detail: string }> {
  if (!API_BASE) return { ok: false, detail: "no backend configured" };
  const res = await fetch(`${API_BASE}/dashboard/resume`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, detail: body.reason || body.detail || `${res.status}` };
}
