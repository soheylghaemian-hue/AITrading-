import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  Watchlist, Opportunities, TradeJournal, PerformanceFull, LearningEngine, Settings, GlobalMarketData,
} from "../components/sections2";
import type { Snapshot } from "../lib/types";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("empty states — never mocks", () => {
  it("Watchlist with no market data → MARKET DATA UNAVAILABLE", () => {
    expect(r(<Watchlist s={{} as Snapshot} />)).toContain("MARKET DATA UNAVAILABLE");
  });
  it("Opportunities with a snapshot but no signals → NO OPPORTUNITIES", () => {
    expect(r(<Opportunities s={{ ai_analysis: [] } as Snapshot} />)).toContain("NO OPPORTUNITIES");
  });
  it("TradeJournal with a snapshot but no trades → NO TRADES", () => {
    expect(r(<TradeJournal s={{ recent_trades: [], n_trades: 0 } as Snapshot} />)).toContain("NO TRADES");
  });
  it("PerformanceFull with no trades → NO DATA (no fake performance)", () => {
    const html = r(<PerformanceFull s={{ n_trades: 0, recent_trades: [], analytics_overall: {} } as Snapshot} />);
    expect(html).toContain("NO DATA");
    expect(html).not.toContain("Win rate");
  });
  it("all sections with null snapshot render NO DATA, not zeros", () => {
    for (const C of [Watchlist, Opportunities, TradeJournal, PerformanceFull, LearningEngine, Settings]) {
      expect(r(<C s={null} />)).toContain("NO DATA");
    }
  });
});

describe("global market data grid (Phase 10)", () => {
  it("shows READY only for realtime and reflects subscription state honestly", () => {
    const s: Snapshot = { global_market_data: [
      { region: "FX", exchange: "IDEALPRO", symbol: "EUR.USD", source: "IDEALPRO", status: "READY",
        realtime: true, bid: 1.152, ask: 1.1521, last: 1.1521, spread: 0.0001, bid_size: 1e6,
        ask_size: 1e6, volume: null, timestamp: "2026-08-13T15:00:00Z", error: null, subscription_state: "ACTIVE" },
      { region: "USA", exchange: "NASDAQ", symbol: "AAPL", source: null, status: "SUBSCRIPTION_REQUIRED",
        realtime: false, bid: null, ask: null, last: null, spread: null, bid_size: null, ask_size: null,
        volume: null, timestamp: null, error: "IBKR 10089 — subscription required", subscription_state: "REQUIRED" },
    ] };
    const html = r(<GlobalMarketData s={s} />);
    expect(html).toContain("EUR.USD");
    expect(html).toContain("READY");
    expect(html).toContain("SUBSCRIPTION_REQUIRED");
    expect(html).toContain("1/2 realtime");
    expect(html).not.toContain("NaN");
  });
  it("null snapshot → NO DATA, never fabricated rows", () => {
    expect(r(<GlobalMarketData s={null} />)).toContain("NO DATA");
  });
});

describe("market-data honesty", () => {
  const s: Snapshot = { system_health: { market_data: "degraded" }, market_data: [
    { symbol: "EUR.USD", asset_class: "fx", exchange: "IDEALPRO", status: "DATA_AVAILABLE",
      market_data_type: "REALTIME", bid: 1.152, ask: 1.1521, last: 1.1521 },
    { symbol: "AAPL", asset_class: "equity", exchange: "NASDAQ", status: "DATA_NOT_AVAILABLE",
      bid: null, ask: null, last: null, error_code: 10089, reason: "subscription required" },
    { symbol: "OLD", asset_class: "equity", status: "STALE", bid: 10, ask: 10.1, last: 10 } as any,
  ] };

  it("REALTIME instrument shows type + price; unavailable shows IBKR 10089, no fake 0", () => {
    const html = r(<Watchlist s={s} />);
    expect(html).toContain("REALTIME");
    expect(html).toContain("1.152");
    expect(html).toContain("IBKR 10089");
    // AAPL null price must not become 0
    expect(html).not.toMatch(/AAPL[\s\S]*?>0<\/td>/);
  });

  it("STALE is a distinct state (never realtime)", () => {
    expect(r(<Watchlist s={s} />)).toContain("STALE");
  });
});

describe("opportunity data-quality gate", () => {
  it("a signal on an instrument WITHOUT market data is not tradable", () => {
    const s: Snapshot = {
      market_data: [{ symbol: "AAPL", status: "DATA_NOT_AVAILABLE", error_code: 10089 } as any],
      ai_analysis: [{ agent: "momentum", instrument: "AAPL:equity", status: "SIGNAL", action: "buy", confidence: 0.8 }],
    };
    const html = r(<Opportunities s={s} />);
    expect(html).toContain("NOT AVAILABLE");
  });
  it("a signal WITH available market data is analyzable, entry/stop/target stay NO DATA", () => {
    const s: Snapshot = {
      market_data: [{ symbol: "EUR.USD", status: "DATA_AVAILABLE", market_data_type: "REALTIME", bid: 1.1, ask: 1.1 } as any],
      ai_analysis: [{ agent: "momentum", instrument: "EUR:fx", status: "SIGNAL", action: "sell", confidence: 0.9 }],
    };
    const html = r(<Opportunities s={s} />);
    expect(html).toContain("ANALYZING");
    expect(html).toContain("NO DATA"); // entry/stop/target never fabricated
  });
});

describe("journal / performance / learning / settings", () => {
  it("TradeJournal renders real trades and shows — for absent fields (no fake P&L)", () => {
    const s: Snapshot = { n_trades: 1, recent_trades: [
      { instrument_key: "EUR.USD:fx", agent: "momentum", quantity: 1000, entry_price: 1.1,
        exit_price: 1.11, realized_pnl: 10, result: "win", mfe: null, mae: null },
    ] };
    const html = r(<TradeJournal s={s} />);
    expect(html).toContain("momentum");
    expect(html).toContain("win");
    expect(html).toContain("—"); // MFE/MAE absent → dash, not 0
  });

  it("PerformanceFull with trades draws an equity curve", () => {
    const s: Snapshot = { n_trades: 3, analytics_overall: { win_rate: 0.66, profit_factor: 2 },
      recent_trades: [{ realized_pnl: 5 }, { realized_pnl: -2 }, { realized_pnl: 8 }] };
    const html = r(<PerformanceFull s={s} />);
    expect(html).toContain("<polyline");
    expect(html).toContain("Win rate");
  });

  it("LearningEngine renders governance lifecycle", () => {
    const s: Snapshot = { governance: [
      { name: "momentum", status: "paper", version: "v1", reason: "", since: "2026-08-12T10:00:00Z" },
    ] };
    const html = r(<LearningEngine s={s} />);
    expect(html).toContain("momentum");
    expect(html.toLowerCase()).toContain("paper");
  });

  it("Settings is read-only with a governance banner and PAPER mode", () => {
    const s: Snapshot = { mode: "paper", execution_enabled: false, orders: 0, risk: { max_daily_loss_pct: 0.03 } };
    const html = r(<Settings s={s} />);
    expect(html).toContain("PAPER");
    expect(html).toContain("DISABLED");
    expect(html.toLowerCase()).toContain("read-only");
  });
});
