import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { OptionsList } from "@/components/terminal/OptionsFeed";
import { fetchOptions } from "@/lib/api";
import { compact, hasOptions, ivPct, premium, scoreTier, sentimentTone, type OptionsData } from "@/lib/options";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const OPT: OptionsData = {
  symbol: "NVDA", options_score: 88, call_put_ratio: 0.37, implied_volatility: 0.42,
  volume: 48000, call_volume: 35000, put_volume: 13000, open_interest: 46000, premium_volume: 19_180_000,
  unusual_activity: "Detected", unusual_activity_score: 72.8, large_trade_count: 4, sentiment: "Bullish",
  signals: ["High call activity", "Large premium trades detected", "Positive positioning"],
  risks: ["Elevated implied volatility"],
};

const EMPTY: OptionsData = {
  symbol: "NVDA", options_score: null, call_put_ratio: null, implied_volatility: null, volume: null,
  call_volume: null, put_volume: null, open_interest: null, premium_volume: null, unusual_activity: null,
  unusual_activity_score: null, large_trade_count: null, sentiment: null, signals: [], risks: [],
};

describe("options helpers", () => {
  it("formats score tier, IV, volume, premium and sentiment tone", () => {
    expect(scoreTier(88)).toBe("hi");
    expect(scoreTier(55)).toBe("med");
    expect(scoreTier(20)).toBe("lo");
    expect(ivPct(0.42)).toBe("42.0%");
    expect(ivPct(null)).toBe("NO DATA");
    expect(compact(48000)).toBe("48.0K");
    expect(premium(19_180_000)).toBe("$19.2M");
    expect(sentimentTone("Bullish")).toBe("pos");
    expect(sentimentTone("Bearish")).toBe("neg");
    expect(hasOptions(EMPTY)).toBe(false);
    expect(hasOptions(OPT)).toBe(true);
  });
});

describe("OptionsList — real flow, never fabricated", () => {
  it("renders score, IV, put/call, volume, premium, unusual activity, signals and risks", () => {
    const h = r(<OptionsList data={OPT} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NVDA Options Intelligence");
    expect(h).toContain("88");                     // options score
    expect(h).toContain("42.0%");                  // IV
    expect(h).toContain("0.37");                   // put/call ratio
    expect(h).toContain("48.0K");                  // volume
    expect(h).toContain("$19.2M");                 // premium
    expect(h).toContain("Detected");               // unusual activity
    expect(h).toContain("Bullish");                // sentiment
    expect(h).toContain("High call activity");     // signal
    expect(h).toContain("Elevated implied volatility"); // risk
  });
  it("shows LOADING while fetching", () => {
    const h = r(<OptionsList data={null} loading error={null} symbol="NVDA" />);
    expect(h).toContain("LOADING");
    expect(h).not.toContain("Bullish");
  });
  it("shows NO DATA on error (nothing invented)", () => {
    const h = r(<OptionsList data={null} loading={false} error="unavailable" symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("unavailable");
  });
  it("shows NO DATA when there is no coverage (no fabricated metrics)", () => {
    const h = r(<OptionsList data={EMPTY} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("No options coverage");
    expect(h).not.toContain("Bullish");
  });
});

describe("fetchOptions — reads only through the same-origin proxy", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/options/{symbol} and parses the data", async () => {
    vi.stubGlobal("fetch", okFetch(OPT));
    const res = await fetchOptions("NVDA");
    expect(calls[0]).toBe("/api/dashboard/options/NVDA");
    expect(res.options_score).toBe(88);
    expect(res.signals.length).toBe(3);
  });
  it("symbol switching NVDA → AAPL fetches distinct proxy URLs", async () => {
    vi.stubGlobal("fetch", okFetch(EMPTY));
    for (const s of ["NVDA", "AAPL"]) await fetchOptions(s);
    expect(calls).toEqual(["/api/dashboard/options/NVDA", "/api/dashboard/options/AAPL"]);
  });
  it("rejects on a non-OK backend response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchOptions("NVDA")).rejects.toBeTruthy();
  });
});
