import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { CandleChart } from "@/components/CandleChart";
import { MarketDetailView } from "@/components/views";
import { ohlcForSymbol, type OhlcBar } from "@/lib/ohlc";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

function mkBars(n: number): OhlcBar[] {
  const bars: OhlcBar[] = [];
  let p = 100;
  for (let i = 0; i < n; i++) {
    const o = p, c = +(p + Math.sin(i / 3)).toFixed(2), h = +(Math.max(o, c) + 0.5).toFixed(2), l = +(Math.min(o, c) - 0.5).toFixed(2);
    bars.push({ timestamp: `2026-08-15T${String(9 + Math.floor(i / 6)).padStart(2, "0")}:${String((i % 6) * 10).padStart(2, "0")}:00Z`, open: o, high: h, low: l, close: c, volume: 1000 + i * 10 });
    p = c;
  }
  return bars;
}

describe("CandleChart — real bars only, computed indicators, guarded overlays", () => {
  const bars = mkBars(30);

  it("renders candlesticks, volume and indicators when OHLC is present", () => {
    const h = r(<CandleChart bars={bars} />);
    expect(h).toContain('data-layer="price"');
    expect(h).toContain('data-layer="indicator"');
    expect(h).toContain("RSI 14");
    expect((h.match(/<rect/g) || []).length).toBeGreaterThan(bars.length); // volume + candle bodies
    expect((h.match(/<polyline/g) || []).length).toBeGreaterThanOrEqual(3); // EMA20 + EMA50 + RSI
  });

  it("NEVER fabricates candles: empty / too-few / malformed bars render nothing", () => {
    expect(r(<CandleChart bars={[]} />)).toBe("");
    expect(r(<CandleChart bars={[bars[0]]} />)).toBe("");
    const malformed = [{ timestamp: "t", open: NaN, high: 1, low: 0, close: 0.5, volume: 1 } as any, { ...bars[0] }];
    expect(r(<CandleChart bars={malformed} />)).toBe(""); // one bad + one good → <2 valid → nothing
  });

  it("draws AI overlays ONLY when the decision fields exist (never calculated)", () => {
    const all = r(<CandleChart bars={bars} ai={{ action: "BUY", entry: 100.5, stop: 98, target: 104 }} />);
    expect(all).toContain("ENTRY 100.50");
    expect(all).toContain("STOP 98.00");
    expect(all).toContain("TARGET 104.00");

    const none = r(<CandleChart bars={bars} ai={null} />);
    expect(none).not.toContain("ENTRY");
    expect(none).not.toContain("STOP");
    expect(none).not.toContain("TARGET");

    const partial = r(<CandleChart bars={bars} ai={{ entry: 100.5 }} />);
    expect(partial).toContain("ENTRY");
    expect(partial).not.toContain("STOP");
    expect(partial).not.toContain("TARGET");
  });
});

describe("/markets/[symbol] wiring — chart vs honest NO DATA", () => {
  const bars = mkBars(30);
  const withOhlc: any = {
    mode: "paper", connected: true, execution_enabled: false,
    global_market_data: [{ region: "USA", symbol: "NVDA", source: "MASSIVE", status: "DATA_AVAILABLE", realtime: true, bid: 100.4, ask: 100.6, last: 100.5, spread: 0.2, bid_size: 1, ask_size: 1, volume: 1, latency_ms: 100, subscription_state: "OK" }],
    autonomous: { status: "ARMED", decisions: [{ ts: "t", instrument: "NVDA", action: "BUY", entry: 100.5, stop: 98, target: 104, reason: "momentum" }] },
    ohlc: { NVDA: { interval: "15m", bars } },
  };

  it("renders the chart when OHLC exists, with AI overlays from real decision fields", () => {
    const h = r(<MarketDetailView s={withOhlc} symbol="NVDA" connected />);
    expect(h).toContain("RSI 14");
    expect(h).toContain('data-layer="price"');
    expect(h).toContain("ENTRY 100.50");
    expect(h).not.toContain("Historical chart unavailable");
  });

  it("shows 'Historical chart unavailable' (NO DATA) when OHLC is missing", () => {
    const noOhlc: any = { ...withOhlc, ohlc: undefined };
    const h = r(<MarketDetailView s={noOhlc} symbol="NVDA" connected />);
    expect(h).toContain("Historical chart unavailable");
    expect(h).not.toContain('data-layer="price"');
  });

  it("disconnected backend → unreachable banner + NO DATA + chart unavailable", () => {
    const h = r(<MarketDetailView s={null} symbol="NVDA" connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
    expect(h).toContain("Historical chart unavailable");
  });
});

describe("ohlcForSymbol — defensive, never fabricated", () => {
  it("returns null for absent/empty and a series for valid (case-insensitive)", () => {
    expect(ohlcForSymbol(null, "NVDA")).toBeNull();
    expect(ohlcForSymbol({ ohlc: {} }, "NVDA")).toBeNull();
    expect(ohlcForSymbol({ ohlc: { NVDA: { interval: "1D", bars: [] } } }, "NVDA")).toBeNull();
    const s = ohlcForSymbol({ ohlc: { NVDA: { interval: "15m", bars: mkBars(3) } } }, "nvda");
    expect(s?.interval).toBe("15m");
    expect(s?.bars.length).toBe(3);
  });
});
