import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { composeSnapshot } from "@/lib/snapshot-compose";
import { SystemView, MarketsView, OverviewView, PortfolioView, RiskView } from "@/components/views";

// Real payload shapes from the ATP Control/Observability API (/status, /broker, /market), captured live.
const STATUS = {
  runtime_state: "DISABLED", kill_switch: false, db: true,
  services: [
    { service: "broker", status: "DEGRADED", detail: "connect failed", age_s: 4, healthy: false },
    { service: "control", status: "UP", detail: "control api", age_s: 2.6, healthy: true },
    { service: "market_data", status: "UP", detail: "feed=STREAMING", age_s: 39282, healthy: false },
    { service: "marketdata_ohlc", status: "UP", detail: "aggregating", age_s: 3.6, healthy: true },
    { service: "trading_core", status: "UP", detail: "state=DISABLED", age_s: 76912, healthy: false },
  ],
  market_data: [{ symbol: "AAPL", source: "MASSIVE", status: "DATA_NOT_AVAILABLE", age_s: 39278, fresh: false }],
  ts: "2026-08-16T07:37:00Z",
};
const BROKER = {
  broker: "IBKR", mode: "PAPER", connection: "DISCONNECTED", account: "DUR849735",
  reconciliation: "UNAVAILABLE", equity: null, cash: null, buying_power: null, currency: null,
  position_count: 0, open_order_count: null, execution_enabled: false, runtime_state: "DISABLED",
  ts: "2026-08-16T07:37:09Z", connection_raw: "DISCONNECTED", heartbeat_age: 4.1, stale_threshold_s: 20,
};
const MARKET = {
  feed: "STREAMING", ts: "2026-08-16T07:37:13Z",
  catalog: { status: "DISCOVERED", regions: { USA: {
    discovered: 13149, ibkr_verified: 0, ready: 0,
    by_type: { STK: 7500, ETF: 5649 }, sources: ["NASDAQ Trader nasdaqlisted"],
  } } },
  market_data: ["AAPL", "NVDA", "SPY"].map((symbol) => ({
    symbol, source: "MASSIVE", status: "DATA_NOT_AVAILABLE", realtime: false,
    bid: null, ask: null, last: null, bid_size: null, ask_size: null, volume: null,
    latency_ms: null, last_update: "2026-08-15T20:42:34Z", fresh: false, error: "no quote",
  })),
};

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("composeSnapshot — maps observability API into the Snapshot (no fabrication)", () => {
  const snap = composeSnapshot(STATUS, BROKER, MARKET);

  it("maps system/mode/execution/broker-link honestly", () => {
    expect(snap.mode).toBe("PAPER");
    expect(snap.system_status).toBe("DISABLED");
    expect(snap.execution_enabled).toBe(false);
    expect(snap.connected).toBe(false);                 // broker DISCONNECTED
    expect(snap.autonomous?.status).toBe("DISABLED");
    expect(snap.autonomous?.ibkr_orders).toBe(0);
    expect(snap.autonomous?.live_execution).toBe(false);
  });

  it("builds system_health from real services + db + broker connection", () => {
    expect(snap.system_health?.market_data).toBe("UP");
    expect(snap.system_health?.trading_core).toBe("UP");
    expect(snap.system_health?.database).toBe("UP");
    expect(snap.system_health?.broker).toBe("DISCONNECTED");
  });

  it("maps market rows without inventing prices", () => {
    expect(snap.global_market_data?.length).toBe(3);
    const nvda = snap.global_market_data?.find((r) => r.symbol === "NVDA");
    expect(nvda?.status).toBe("DATA_NOT_AVAILABLE");
    expect(nvda?.last).toBeNull();                      // no fabricated price
    expect(nvda?.bid).toBeNull();
  });

  it("maps discovered catalogue counts separately from verified and ready contracts", () => {
    expect(snap.market_catalog?.regions?.USA.discovered).toBe(13149);
    expect(snap.market_catalog?.regions?.USA.ibkr_verified).toBe(0);
    expect(snap.market_catalog?.regions?.USA.ready).toBe(0);
  });

  it("leaves unavailable domains as NO DATA (risk, positions, account equity)", () => {
    expect(snap.trading_risk).toBeNull();
    expect(snap.positions).toEqual([]);
    expect(snap.account?.equity).toBeNull();
  });

  it("never throws on empty/missing backend payloads", () => {
    const empty = composeSnapshot({}, {}, {});
    expect(empty.global_market_data).toEqual([]);
    expect(empty.account?.equity).toBeNull();
    expect(empty.autonomous?.status).toBe("DISABLED");
    expect(empty.trading_risk).toBeNull();
  });
});

describe("composed snapshot drives the real views with live values", () => {
  const snap = composeSnapshot(STATUS, BROKER, MARKET);

  it("System view shows real service health + broker DISCONNECTED, not NO DATA", () => {
    const h = r(<SystemView s={snap} connected />);
    expect(h).toContain("Broker Connector");
    expect(h).toContain("DISCONNECTED");
    expect(h).toContain("Market Data");
    expect(h).toContain("UP");
    expect(h).toContain("false");   // Advanced diagnostics: execution enabled = false
  });

  it("Markets view lists real instruments with honest DATA_NOT_AVAILABLE status", () => {
    const h = r(<MarketsView s={snap} connected />);
    expect(h).toContain("NVDA");
    expect(h).toContain("AAPL");
    expect(h).toContain("SPY");
    expect(h).toContain("Global Instrument Catalog");
    expect(h).toContain("13,149");
    expect(h).toContain("NOT YET TRADEABLE");
  });

  it("Overview shows PAPER account + DISABLED engine (no fabricated equity/PNL)", () => {
    const h = r(<OverviewView s={snap} connected />);
    expect(h).toContain("PAPER account");
    expect(h).toContain("DISABLED");
  });
});

// Phase G1.8 — the /dashboard read-model (account P&L, positions, risk, AI) from persisted state.
const BROKER_CONNECTED = { ...BROKER, connection: "CONNECTED", equity: 1000000, cash: 500000, currency: "EUR" };
const DASHBOARD = {
  account: { equity: 1000000, cash: 500000, pnl: -8200, currency: "EUR", connected: true },
  positions: [{ symbol: "AAPL", quantity: 100, avg_price: 150.25, pnl: 1234.5, updated_at: "2026-08-16T09:00:00Z" }],
  risk: {
    capital: 1000000, risk_per_trade_pct: 0.01, max_daily_loss_pct: 0.03,
    day_start_equity: 1000000, peak_equity: 1010000, daily_pnl: -8200,
    daily_loss_pct: 0.0082, drawdown: 0.0099, halted: false, killed: false,
  },
  system: { recovery_state: "DISABLED", recovery_reason: null },
  ai: {
    decisions: [{
      decision_id: "d1", ts: "2026-08-16T10:00:00Z", instrument: "NVDA",
      action: "BUY", confidence: 0.87, entry: 100.5, stop: 98, target: 104, final_decision: "APPROVED",
    }],
  },
  ts: "2026-08-16T12:00:00Z",
};

describe("composeSnapshot — merges the /dashboard read-model (never fabricates)", () => {
  it("maps account P&L, positions, risk and AI decisions from persisted state", () => {
    const snap = composeSnapshot(STATUS, BROKER_CONNECTED, MARKET, DASHBOARD);
    expect(snap.account?.equity).toBe(1000000);
    expect(snap.account?.pnl).toBe(-8200);
    expect(snap.positions?.length).toBe(1);
    expect(snap.positions?.[0]).toMatchObject({ symbol: "AAPL", quantity: 100, avg_price: 150.25, pnl: 1234.5 });
    expect(snap.risk?.drawdown).toBe(0.0099);
    expect(snap.trading_risk?.capital).toBe(1000000);
    expect(snap.trading_risk?.max_daily_loss).toBe(30000);
    expect(snap.trading_risk?.max_risk_per_trade).toBe(10000);
    expect(snap.trading_risk?.remaining_daily_risk).toBe(21800);   // 30000 - 8200 loss
    expect(snap.trading_risk?.status).toBe("ACTIVE");
    expect(snap.autonomous?.decisions?.length).toBe(1);
    expect(snap.autonomous?.decisions?.[0]).toMatchObject({ instrument: "NVDA", action: "BUY" });
  });

  it("without a dashboard payload the snapshot is unchanged (graceful NO DATA)", () => {
    const snap = composeSnapshot(STATUS, BROKER, MARKET);            // 3 args → no dashboard
    expect(snap.positions).toEqual([]);
    expect(snap.trading_risk).toBeNull();
    expect(snap.risk).toBeUndefined();
    expect(snap.account?.pnl).toBeUndefined();
    expect(snap.autonomous?.decisions).toEqual([]);
  });

  it("drives Portfolio / Risk / Overview with real persisted values", () => {
    const snap = composeSnapshot(STATUS, BROKER_CONNECTED, MARKET, DASHBOARD);
    expect(r(<PortfolioView s={snap} connected />)).toContain("AAPL");     // real position
    expect(r(<RiskView s={snap} connected />)).toContain("HEALTHY");        // riskHealth from real limits
    expect(r(<OverviewView s={snap} connected />)).toContain("NVDA");       // last AI decision
  });
});
