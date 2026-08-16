import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Snapshot } from "@/lib/types";
import {
  OverviewView, MarketsView, PortfolioView, AiBrainView, RiskView, SystemView,
} from "@/components/views";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const rich: Snapshot = {
  mode: "paper", system_status: "online", connected: true, execution_enabled: false,
  account: { equity: 1_024_500, cash: 486_300, gross_exposure: 538_200, net_exposure: 538_200 },
  trading_risk: {
    capital: 1_000_000, risk_per_trade_pct: 0.01, max_risk_per_trade: 10_000,
    max_daily_loss_pct: 0.03, max_daily_loss: 30_000, current_daily_pnl: -8_200,
    remaining_daily_risk: 21_800, status: "ACTIVE",
  },
  autonomous: {
    mode: "paper", status: "ARMED", paper_equity: 1_024_500, today_pnl: 4_820, open_positions: 2,
    trades_today: 3, risk_used: 8_200, remaining_daily_loss: 21_800, max_daily_loss: 30_000,
    live_execution: false, ibkr_orders: 0,
    metrics: {
      total_evaluations: 1_284, opportunities_detected: 3, potential_trades: 5, approved_decisions: 42,
      rejected_decisions: 18, no_data_decisions: 4, risk_vetoes: 6, avg_confidence: 0.7,
      avg_expected_risk: null, avg_suggested_position: null, signals_by_instrument: {}, signals_by_agent: {},
    },
    decisions: [{
      ts: "2026-08-15T14:30:00Z", instrument: "NVDA", agent: "momentum", action: "BUY", confidence: 0.87,
      entry: 225.30, stop: 221.80, target: 233.00, monetary_risk: 1_000, risk_decision: "APPROVED",
      regime: "trending_up", reason: "Strong momentum with expanding volume",
    }],
  } as any,
  positions: [{ symbol: "NVDA", quantity: 885, avg_price: 225.30, market_price: 226.40, unrealized_pnl: 1_240, monetary_risk: 1_000 }],
  global_market_data: [{
    region: "USA", symbol: "NVDA", source: "MASSIVE", status: "DATA_AVAILABLE", realtime: true,
    bid: 226.38, ask: 226.42, last: 226.40, spread: 0.04, bid_size: 2_400, ask_size: 1_800,
    volume: 38_200_000, latency_ms: 101, subscription_state: "OK",
  }],
  system_health: { market_data: "healthy", trading_core: "healthy", risk: "healthy", broker: "healthy", database: "healthy", redis: "healthy" },
};

// market-data (IBKR-style) snapshot exercising the error-translation + honest NO DATA path
const mdErr: Snapshot = {
  mode: "paper", connected: true, execution_enabled: false,
  market_data: [
    { symbol: "EUR.USD", status: "DATA_AVAILABLE", market_data_type: "REALTIME", bid: 1.15234, ask: 1.15235, last: null },
    { symbol: "AAPL", status: "DATA_NOT_AVAILABLE", bid: null, ask: null, last: null, error_code: 10089, reason: "subscription required" },
  ],
};

describe("every route renders (with data)", () => {
  it("Overview shows equity, engine state, and a position", () => {
    const h = r(<OverviewView s={rich} connected />);
    expect(h).toContain("$1,024,500");
    expect(h).toContain("AI Trading Engine");
    expect(h).toContain("ARMED");
    expect(h).toContain("NVDA");
    expect(h).toContain("Market Regime");
    expect(h).toContain("Trending Up");   // read from decisions[].regime, never computed
  });
  it("Markets shows source, human status and a realtime quote", () => {
    const h = r(<MarketsView s={rich} connected />);
    expect(h).toContain("MASSIVE");
    expect(h).toContain("Live");        // humanStatus(DATA_AVAILABLE)
    expect(h).toContain("101ms");
  });
  it("Portfolio shows capital, exposure and positions", () => {
    const h = r(<PortfolioView s={rich} connected />);
    expect(h).toContain("$1,024,500");
    expect(h).toContain("$486,300");
    expect(h).toContain("Exposure");
    expect(h).toContain("NVDA");
  });
  it("AI Brain shows conviction, decision, verdict, agent breakdown and metrics", () => {
    const h = r(<AiBrainView s={rich} connected />);
    expect(h).toContain("1,284");
    expect(h).toContain("BUY");
    expect(h).toContain("87%");
    expect(h).toContain("APPROVED");
    expect(h).toContain("WHY?");
  });
  it("Risk Center shows a health indicator and the mandate", () => {
    const h = r(<RiskView s={rich} connected />);
    expect(h).toContain("HEALTHY");
    expect(h).toContain("$1,000,000");
    expect(h).toContain("1.0%");
    expect(h).toContain("3.0%");
  });
  it("Risk Center blocks when the daily loss limit is reached", () => {
    const blocked: Snapshot = { ...rich, trading_risk: { ...rich.trading_risk!, status: "DAILY LOSS LIMIT REACHED" } };
    expect(r(<RiskView s={blocked} connected />)).toContain("BLOCKED");
  });
  it("System shows health services and Advanced Diagnostics", () => {
    const h = r(<SystemView s={rich} connected />);
    expect(h).toContain("Market Data");
    expect(h).toContain("HEALTHY");
    expect(h).toContain("Advanced Diagnostics");
  });
});

describe("Markets symbol navigation (§ G2.1 fix)", () => {
  // Market closed → every symbol is DATA_NOT_AVAILABLE, but each must STILL link to its detail terminal
  // (News + research work independent of market hours). This is the regression that broke navigation.
  const closed: Snapshot = {
    mode: "paper", connected: true, execution_enabled: false,
    global_market_data: ["NVDA", "AAPL", "SPY"].map((symbol) => ({
      region: "USA", symbol, source: "MASSIVE", status: "DATA_NOT_AVAILABLE", realtime: false,
      bid: null, ask: null, last: null, spread: null, bid_size: null, ask_size: null, volume: null,
      subscription_state: "OK",
    })),
  } as any;

  it("links every symbol to /markets/[symbol] even when market data is unavailable", () => {
    const h = r(<MarketsView s={closed} connected />);
    for (const sym of ["NVDA", "AAPL", "SPY"]) {
      expect(h).toContain(`<a href="/markets/${sym}">${sym}</a>`);   // NVDA → /markets/NVDA, etc.
    }
    expect(h).not.toMatch(/<td>NVDA<\/td>/);                          // never an unlinked plain-text symbol
  });

  it("still links the symbol when a live quote IS available", () => {
    const h = r(<MarketsView s={rich} connected />);                  // rich: NVDA DATA_AVAILABLE
    expect(h).toContain('<a href="/markets/NVDA">NVDA</a>');
  });
});

describe("NO DATA discipline (null snapshot) — no fabricated values", () => {
  const cases: [string, React.ReactElement][] = [
    ["Overview", <OverviewView s={null} connected={false} />],
    ["Markets", <MarketsView s={null} connected={false} />],
    ["Portfolio", <PortfolioView s={null} connected={false} />],
    ["AiBrain", <AiBrainView s={null} connected={false} />],
    ["Risk", <RiskView s={null} connected={false} />],
    ["System", <SystemView s={null} connected={false} />],
  ];
  for (const [name, el] of cases) {
    it(`${name} renders NO DATA and never a fabricated $0`, () => {
      const h = r(el);
      expect(h).toContain("NO DATA");
      expect(h).not.toContain("$0.00");
      expect(h).not.toMatch(/>0<\/(td|div|span)>/); // no invented zero cells
    });
  }
});

describe("disconnected backend state", () => {
  it("shows the unreachable banner and NO DATA, never fabricated numbers", () => {
    const h = r(<OverviewView s={null} connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
  });
});

describe("market-data honesty + error translation", () => {
  it("realtime row shows the real quote; unavailable row shows a friendly reason, no fake price", () => {
    const h = r(<MarketsView s={mdErr} connected />);
    expect(h).toContain("1.15234");                 // real bid
    expect(h).toContain("Market data unavailable"); // AAPL friendly status (not the raw enum)
    expect(h).toContain("IBKR 10089");              // raw code only in the details disclosure
    // AAPL had null bid/ask/last → must not print a fabricated 0 price
    expect(h).not.toMatch(/AAPL[\s\S]{0,200}?>0<\/td>/);
  });
});
