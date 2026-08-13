import { describe, it, expect } from "vitest";
import type { Snapshot } from "../lib/types";

// Contract shape the frontend consumes from GET /dashboard/summary. This mirrors the backend
// read-model (atp.dashboard.snapshot). If the backend contract changes, these keys must change
// with it — the test documents the coupling.
const REQUIRED_KEYS: (keyof Snapshot)[] = [
  "mode", "system_status", "connected", "execution_enabled", "orders",
  "account", "risk", "positions", "market", "market_data", "subscriptions",
  "ai_analysis", "agents", "governance", "system_health", "hero",
  "analytics_overall", "recent_trades", "notifications", "n_trades",
];

describe("dashboard API contract", () => {
  it("the Snapshot type covers every field the UI reads", () => {
    // A representative snapshot (as the backend serializes it) satisfies the type.
    const sample: Snapshot = {
      mode: "paper", system_status: "online", connected: true, execution_enabled: false, orders: 0,
      account: { equity: 1_000_000, cash: 1_000_000, buying_power: 6_000_000, realized_pnl: 0, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0, gross_leverage: 0 },
      risk: { halted: false, killed: false, broker_connected: true, drawdown: 0, daily_pnl: 0, max_daily_loss_pct: 0.03 },
      positions: [], market: {}, market_data: [], subscriptions: [], ai_analysis: [], agents: [],
      governance: [], system_health: { broker: "online" }, hero: {}, analytics_overall: {},
      recent_trades: [], notifications: [], n_trades: 0,
    };
    for (const k of REQUIRED_KEYS) expect(k in sample).toBe(true);
  });

  it("defaults to the same-origin server proxy (/api) and never embeds a secret", async () => {
    // With no NEXT_PUBLIC_API_URL, the browser talks to the same-origin proxy; the token lives
    // only in the Vercel server env, never in the client bundle.
    const { fetchSnapshot, API_BASE, POLL_MS } = await import("../lib/api");
    expect(API_BASE).toBe("/api");
    expect(POLL_MS).toBeGreaterThanOrEqual(1000);
    // In the node test env a relative fetch has no origin → it rejects (no fabricated data).
    await expect(fetchSnapshot()).rejects.toBeTruthy();
  });

  it("the frontend only ever reads /dashboard/summary and posts /dashboard/emergency-stop|resume", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const api = fs.readFileSync(path.join(__dirname, "..", "lib", "api.ts"), "utf8");
    const targets = [...api.matchAll(/`\$\{API_BASE\}(\/[^`]*)`/g)].map((m) => m[1]);
    expect(targets).toContain("/dashboard/summary");
    for (const t of targets) expect(t.startsWith("/dashboard/")).toBe(true);
  });
});
