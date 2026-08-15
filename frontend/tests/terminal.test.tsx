import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketTerminal } from "@/components/terminal/MarketTerminal";
import { TerminalHeader } from "@/components/terminal/TerminalHeader";
import { AiAnalysisPanel } from "@/components/terminal/AiAnalysisPanel";
import { EventTimeline } from "@/components/terminal/EventTimeline";
import { DataQuality } from "@/components/terminal/DataQuality";
import { ema, vwap, macd, rsi } from "@/lib/indicators";
import type { OhlcBar } from "@/lib/ohlc";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

function mkBars(n: number): OhlcBar[] {
  const bars: OhlcBar[] = [];
  let p = 100;
  for (let i = 0; i < n; i++) {
    const o = p, c = +(p + Math.sin(i / 3)).toFixed(2), h = +(Math.max(o, c) + 0.5).toFixed(2), l = +(Math.min(o, c) - 0.5).toFixed(2);
    bars.push({ timestamp: `2026-08-15T14:${String((i % 6) * 10).padStart(2, "0")}:00Z`, open: o, high: h, low: l, close: c, volume: 1000 + i * 10 });
    p = c;
  }
  return bars;
}

const bars = mkBars(40);
const base: any = {
  mode: "paper", connected: true, execution_enabled: false,
  global_market_data: [{ region: "USA", symbol: "NVDA", source: "MASSIVE", status: "DATA_AVAILABLE", realtime: true, bid: 100.4, ask: 100.6, last: 100.5, spread: 0.2, bid_size: 1, ask_size: 1, volume: 38200000, latency_ms: 72, timestamp: "2026-08-15T14:30:00Z", subscription_state: "OK" }],
  trading_risk: { capital: 1000000, risk_per_trade_pct: 0.01, max_risk_per_trade: 10000, max_daily_loss_pct: 0.03, max_daily_loss: 30000, current_daily_pnl: -8200, remaining_daily_risk: 21800, status: "ACTIVE" },
  autonomous: { status: "ARMED", decisions: [{ ts: "2026-08-15T14:30:00Z", instrument: "NVDA", action: "BUY", confidence: 0.87, entry: 100.5, stop: 98, target: 104, monetary_risk: 1000, risk_decision: "APPROVED", agents: { momentum: "BUY 0.87", risk: "WITHIN LIMITS", macro: "SUPPORTIVE" }, reason: "momentum confirmation" }] },
  ohlc: { NVDA: { interval: "15m", bars } },
};

describe("indicators — computed from real closes, never fabricated", () => {
  it("ema/vwap/macd/rsi are deterministic transforms of the input only", () => {
    expect(ema(3, [])).toEqual([]);
    ema(2, [5, 5, 5, 5]).forEach((v) => expect(v).toBeCloseTo(5, 9));      // constant in → constant out
    expect(vwap([{ high: 10, low: 10, close: 10, volume: 100 }, { high: 10, low: 10, close: 10, volume: 100 }] as any)).toEqual([10, 10]);
    expect(macd(new Array(30).fill(1)).hist.every((v) => Math.abs(v) < 1e-9)).toBe(true);
    expect(rsi(14, [1, 2]).every((v) => v === null)).toBe(true);            // too few → no invented RSI
  });
});

describe("MarketTerminal /markets/[symbol]", () => {
  it("renders the terminal + chart when OHLC exists, with AI overlays from real decision fields", () => {
    const h = r(<MarketTerminal s={base} symbol="NVDA" connected />);
    expect(h).toContain("NVIDIA Corporation");
    expect(h).toContain('data-layer="price"');
    expect(h).toContain("MACD 12/26/9");
    expect(h).toContain("ENTRY 100.50");
    expect(h).toContain("AI Market Analysis");
    expect(h).not.toContain("HISTORICAL DATA NOT CONNECTED");
  });

  it("shows HISTORICAL DATA NOT CONNECTED when OHLC is missing (no fabricated candles)", () => {
    const h = r(<MarketTerminal s={{ ...base, ohlc: undefined }} symbol="NVDA" connected />);
    expect(h).toContain("HISTORICAL DATA NOT CONNECTED");
    expect(h).not.toContain('data-layer="price"');
  });

  it("AI overlays only render when the decision has entry/stop/target", () => {
    const h = r(<MarketTerminal s={{ ...base, autonomous: { status: "ARMED", decisions: [] } }} symbol="NVDA" connected />);
    expect(h).not.toContain("ENTRY 100.50");
    expect(h).toContain("NO DATA");
  });

  it("disconnected backend → unreachable banner + NO DATA + chart not connected", () => {
    const h = r(<MarketTerminal s={null} symbol="NVDA" connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
    expect(h).toContain("HISTORICAL DATA NOT CONNECTED");
  });

  it("Paper/Live mode is visible in the header", () => {
    expect(r(<TerminalHeader symbol="NVDA" refData={null} quote={null} mode="paper" change={null} connected />)).toContain("PAPER");
    expect(r(<TerminalHeader symbol="NVDA" refData={null} quote={null} mode="LIVE" change={null} connected />)).toContain("LIVE");
  });
});

describe("AI panel + event timeline honesty", () => {
  it("shows real fields and NO DATA for missing agents", () => {
    const h = r(<AiAnalysisPanel dec={base.autonomous.decisions[0]} risk={base.trading_risk} mode="paper" executionEnabled={false} />);
    expect(h).toContain("87");
    expect(h).toContain("BUY");
    expect(h).toContain("APPROVED");
    expect(h).toContain("PAPER");
    expect(h).toContain("DISABLED");
    expect(h).toContain("News Agent");
    expect(h).toContain("NO DATA");           // News agent has no data
  });
  it("is all NO DATA without a decision", () => {
    const h = r(<AiAnalysisPanel dec={null} risk={null} mode="paper" executionEnabled={false} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("APPROVED");
  });
  it("event timeline shows NO DATA when empty (prepared for future sources)", () => {
    expect(r(<EventTimeline events={[]} />)).toContain("NO DATA");
    expect(r(<EventTimeline events={[{ ts: "2026-08-15T14:30:00Z", kind: "signal", title: "BUY NVDA", tone: "ok" }]} />)).toContain("AI SIGNAL");
  });
});

describe("Market Data Quality — existing fields only, never calculated", () => {
  it("shows source / status / latency / timestamp / exchange when present", () => {
    const q: any = { symbol: "NVDA", source: "MASSIVE", realtime: true, bid: 100.4, ask: 100.6, last: 100.5, volume: 1, latency: 72, status: "DATA_AVAILABLE", code: null, reason: null, region: "USA", timestamp: "2026-08-15T14:30:00Z" };
    const h = r(<DataQuality quote={q} refData={{ company: "NVIDIA Corporation", exchange: "NASDAQ" }} />);
    expect(h).toContain("Market Data Quality");
    expect(h).toContain("MASSIVE");
    expect(h).toContain("72ms");
    expect(h).toContain("14:30:00");
    expect(h).toContain("NASDAQ");
  });
  it("renders NO DATA for every missing field (no fabricated values)", () => {
    const h = r(<DataQuality quote={null} refData={null} />);
    expect(h).toContain("Market Data Quality");
    expect((h.match(/NO DATA/g) || []).length).toBeGreaterThanOrEqual(5);
  });
});
