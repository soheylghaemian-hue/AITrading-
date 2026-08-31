import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ConsensusView } from "@/components/ConsensusPanel";
import { AiSummary } from "@/components/terminal/AiSummary";
import { fetchConsensus } from "@/lib/api";
import { directionTone, hasConsensus, scoreTier, type AiConsensus } from "@/lib/consensus";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const CON: AiConsensus = {
  symbol: "NVDA", score: 89, direction: "BULLISH", confidence: 89, status: "COMPLETE", coverage: 1.0,
  components: [
    { component_name: "Market Data", score: 85, weight: 0.20, direction: "bullish", reason: "Positive momentum", risk_flags: [] },
    { component_name: "News", score: 92, weight: 0.15, direction: "bullish", reason: "Positive news", risk_flags: [] },
    { component_name: "Fundamentals", score: 93, weight: 0.20, direction: "bullish", reason: "quality 93", risk_flags: [] },
    { component_name: "Options", score: 88, weight: 0.15, direction: "bullish", reason: "Bullish", risk_flags: [] },
    { component_name: "Trader Intelligence", score: 82, weight: 0.15, direction: "bullish", reason: "BULLISH", risk_flags: [] },
    { component_name: "Risk", score: 91, weight: 0.15, direction: "neutral", reason: "healthy", risk_flags: [] },
  ],
  strengths: ["Revenue growth", "Positive news flow", "Bullish trader consensus"],
  risks: ["High valuation"],
  conflicts: [],
};
const CONFLICT: AiConsensus = { ...CON, direction: "NEUTRAL", conflicts: ["News bullish but Options bearish"] };
const PARTIAL: AiConsensus = { ...CON, status: "PARTIAL" };
const EMPTY: AiConsensus = {
  symbol: "NVDA", score: null, direction: null, confidence: null, status: "NO DATA", coverage: 0,
  components: [], strengths: [], risks: [], conflicts: [],
};

describe("consensus helpers", () => {
  it("maps direction/score to tone/tier + detects data", () => {
    expect(directionTone("BULLISH")).toBe("pos");
    expect(directionTone("BEARISH")).toBe("neg");
    expect(directionTone("NEUTRAL")).toBe("neu");
    expect(scoreTier(89)).toBe("hi");
    expect(scoreTier(50)).toBe("med");
    expect(scoreTier(null)).toBe("lo");
    expect(hasConsensus(EMPTY)).toBe(false);
    expect(hasConsensus(CON)).toBe(true);
  });
});

describe("ConsensusView — transparent AI market view, never fabricated", () => {
  it("renders score, direction, confidence, components, strengths and risks", () => {
    const h = r(<ConsensusView data={CON} loading={false} error={null} />);
    expect(h).toContain("GIGBAY AI Conviction");
    expect(h).toContain("89");                      // conviction score + confidence
    expect(h).toContain("BULLISH");
    expect(h).toContain("Confidence");
    expect(h).toContain("Market Data");
    expect(h).toContain("Fundamentals");
    expect(h).toContain("93");                       // fundamentals component score
    expect(h).toContain("Revenue growth");           // strength
    expect(h).toContain("High valuation");           // risk
    expect(h).not.toContain("CONFLICT DETECTED");
  });
  it("SURFACES conflicts (never hides disagreement) and leans NEUTRAL", () => {
    const h = r(<ConsensusView data={CONFLICT} loading={false} error={null} />);
    expect(h).toContain("CONFLICT DETECTED");
    expect(h).toContain("News bullish but Options bearish");
    expect(h).toContain("NEUTRAL");
  });
  it("flags a PARTIAL assessment", () => {
    expect(r(<ConsensusView data={PARTIAL} loading={false} error={null} />)).toContain("PARTIAL ASSESSMENT");
  });
  it("shows NO DATA when there is no intelligence (nothing fabricated)", () => {
    const h = r(<ConsensusView data={EMPTY} loading={false} error={null} />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("No intelligence available");
    expect(h).not.toContain("BULLISH");
  });
  it("shows LOADING while computing", () => {
    expect(r(<ConsensusView data={null} loading error={null} />)).toContain("LOADING");
  });
});

describe("AiSummary — terminal AI View card", () => {
  it("renders direction, confidence, drivers and main risk", () => {
    const h = r(<AiSummary data={CON} />);
    expect(h).toContain("AI View");
    expect(h).toContain("BULLISH");
    expect(h).toContain("89%");
    expect(h).toContain("Revenue growth");           // driver
    expect(h).toContain("High valuation");           // main risk
  });
  it("shows NO DATA without a consensus", () => {
    expect(r(<AiSummary data={EMPTY} />)).toContain("NO DATA");
  });
});

describe("fetchConsensus — reads only through the same-origin proxy", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/ai-consensus/{symbol} and parses the view", async () => {
    vi.stubGlobal("fetch", okFetch(CON));
    const res = await fetchConsensus("NVDA");
    expect(calls[0]).toBe("/api/dashboard/ai-consensus/NVDA");
    expect(res.score).toBe(89);
    expect(res.direction).toBe("BULLISH");
    expect(res.components.length).toBe(6);
  });
  it("rejects on a non-OK backend response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchConsensus("NVDA")).rejects.toBeTruthy();
  });
});
