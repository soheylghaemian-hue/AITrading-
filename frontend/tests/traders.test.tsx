import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TradersList } from "@/components/terminal/TradersFeed";
import { AiAnalysisPanel } from "@/components/terminal/AiAnalysisPanel";
import { fetchTraders } from "@/lib/api";
import { consensusTone, directionTone, hasTraderData, qualityTier, type TraderConsensus } from "@/lib/traders";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const CON: TraderConsensus = {
  symbol: "NVDA", consensus: "BULLISH", long_percent: 85.6, short_percent: 14.4, neutral_percent: 0,
  weighted_score: 49.1, contributor_count: 3,
  contributors: [
    { id: "B", name: "BetaSteady", quality: 83.6, strategy: "US Technology", market_focus: "US Technology", direction: "LONG" },
    { id: "A", name: "AlphaAggressive", quality: 42.6, strategy: "US Technology", market_focus: "US Technology", direction: "LONG" },
    { id: "C", name: "GammaNoise", quality: 21.2, strategy: "US Technology", market_focus: "US Technology", direction: "SHORT" },
  ],
};

const EMPTY: TraderConsensus = {
  symbol: "NVDA", consensus: null, long_percent: null, short_percent: null, neutral_percent: null,
  weighted_score: null, contributor_count: 0, contributors: [],
};

describe("trader helpers", () => {
  it("maps consensus/direction/quality to tone + tier", () => {
    expect(consensusTone("BULLISH")).toBe("pos");
    expect(consensusTone("BEARISH")).toBe("neg");
    expect(consensusTone(null)).toBe("neu");
    expect(directionTone("LONG")).toBe("pos");
    expect(directionTone("SHORT")).toBe("neg");
    expect(qualityTier(83.6)).toBe("hi");
    expect(qualityTier(60)).toBe("med");
    expect(qualityTier(20)).toBe("lo");
    expect(qualityTier(null)).toBe("lo");
    expect(hasTraderData(EMPTY)).toBe(false);
    expect(hasTraderData(CON)).toBe(true);
  });
});

describe("TradersList — quality-weighted consensus, never fabricated", () => {
  it("renders consensus, directional shares and ranked contributors", () => {
    const h = r(<TradersList data={CON} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NVDA Trader Intelligence");
    expect(h).toContain("BULLISH");
    expect(h).toContain("LONG");
    expect(h).toContain("85.6%");
    expect(h).toContain("SHORT");
    expect(h).toContain("14.4%");
    expect(h).toContain("BetaSteady");
    expect(h).toContain("Quality 83.6/100");   // quality score displayed
    expect(h).toContain("US Technology");       // strategy displayed
    expect(h).toContain("AlphaAggressive");
  });
  it("shows LOADING while fetching (no consensus yet)", () => {
    const h = r(<TradersList data={null} loading error={null} symbol="NVDA" />);
    expect(h).toContain("LOADING");
    expect(h).not.toContain("BULLISH");
  });
  it("shows NO DATA on error (nothing invented)", () => {
    const h = r(<TradersList data={null} loading={false} error="unavailable" symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("unavailable");
  });
  it("shows NO DATA when there is no trader coverage (no fabricated consensus)", () => {
    const h = r(<TradersList data={EMPTY} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("No professional-trader coverage");
    expect(h).not.toContain("BULLISH");
  });
});

describe("AI Brain conviction inputs (§ G2.5)", () => {
  it("renders Trader Consensus as a real input and the rest as NO DATA (no fabricated total)", () => {
    const inputs = [
      { label: "Price Action", value: null },
      { label: "News", value: null },
      { label: "Trader Consensus", value: 82 },
      { label: "Macro", value: null },
    ];
    const h = r(<AiAnalysisPanel dec={null} risk={null} mode="paper" executionEnabled={false} convictionInputs={inputs} />);
    expect(h).toContain("Conviction Inputs");
    expect(h).toContain("Trader Consensus");
    expect(h).toContain("82");                  // the real trader-consensus input value
    expect(h).toContain("Price Action");
    expect(h).toContain("Macro");
  });
});

describe("fetchTraders — reads only through the same-origin proxy", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/traders/{symbol} and parses the consensus", async () => {
    vi.stubGlobal("fetch", okFetch(CON));
    const res = await fetchTraders("NVDA");
    expect(calls[0]).toBe("/api/dashboard/traders/NVDA");
    expect(res.consensus).toBe("BULLISH");
    expect(res.contributors.length).toBe(3);
  });
  it("symbol switching NVDA → AAPL fetches distinct proxy URLs", async () => {
    vi.stubGlobal("fetch", okFetch(EMPTY));
    for (const s of ["NVDA", "AAPL"]) await fetchTraders(s);
    expect(calls).toEqual(["/api/dashboard/traders/NVDA", "/api/dashboard/traders/AAPL"]);
  });
  it("rejects on a non-OK backend response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchTraders("NVDA")).rejects.toBeTruthy();
  });
  it("returns an empty consensus (not an error) when there is no coverage", async () => {
    vi.stubGlobal("fetch", okFetch(EMPTY));
    expect((await fetchTraders("NVDA")).contributor_count).toBe(0);
  });
});
