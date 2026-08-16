import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { GovernanceView } from "@/components/GovernancePanel";
import { fetchGovernance, fetchGovernanceFeed } from "@/lib/api";
import { govTone, hasGovernance, reasonText, type Governance } from "@/lib/governance";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const base: Governance = {
  symbol: "NVDA", status: "APPROVED", score: 89, confidence: 82, data_completeness: 94,
  reasons: [], approved: true, direction: "BULLISH", missing: [], conflicts: [],
};
const APPROVED = base;
const PARTIAL: Governance = {
  ...base, status: "PARTIAL", score: 84, confidence: 76, data_completeness: 85, approved: false,
  reasons: ["MISSING_OPTIONS"], missing: ["Options"],
};
const CONFLICT: Governance = {
  ...base, status: "CONFLICT", score: 70, confidence: 70, data_completeness: 50, approved: false,
  reasons: ["SOURCE_CONFLICT"], conflicts: ["Fundamentals bullish vs Options bearish"],
};
const BLOCKED: Governance = {
  ...base, status: "BLOCKED", score: 60, confidence: 42, data_completeness: 40, approved: false,
  reasons: ["LOW_CONFIDENCE"],
};
const EMPTY: Governance = {
  symbol: "NVDA", status: null, score: null, confidence: null, data_completeness: null,
  reasons: [], approved: false, direction: null, missing: [], conflicts: [],
};

describe("governance helpers", () => {
  it("detects a real verdict + maps tone/reasons", () => {
    expect(hasGovernance(APPROVED)).toBe(true);
    expect(hasGovernance(EMPTY)).toBe(false);
    expect(govTone("APPROVED")).toBe("approved");
    expect(govTone("PARTIAL")).toBe("partial");
    expect(govTone("CONFLICT")).toBe("conflict");
    expect(govTone("BLOCKED")).toBe("blocked");
    expect(reasonText("MISSING_OPTIONS")).toContain("Options");
    expect(reasonText("SOURCE_CONFLICT")).toContain("disagree");
  });
});

describe("GovernanceView — deterministic verdict, never fabricated", () => {
  it("APPROVED shows conviction, confidence, completeness and the ready state", () => {
    const h = r(<GovernanceView data={APPROVED} loading={false} error={null} />);
    expect(h).toContain("AI Decision Governance");
    expect(h).toContain("APPROVED");
    expect(h).toContain("89");                        // conviction
    expect(h).toContain("82%");                       // confidence
    expect(h).toContain("94%");                       // data completeness
    expect(h).toContain("Ready");
    expect(h).toContain("govb approved");
  });
  it("PARTIAL surfaces the missing source", () => {
    const h = r(<GovernanceView data={PARTIAL} loading={false} error={null} />);
    expect(h).toContain("PARTIAL");
    expect(h).toContain("Options data missing");
    expect(h).toContain("Not ready");
  });
  it("CONFLICT surfaces the disagreement (never hidden)", () => {
    const h = r(<GovernanceView data={CONFLICT} loading={false} error={null} />);
    expect(h).toContain("CONFLICT");
    expect(h).toContain("Fundamentals bullish vs Options bearish");
  });
  it("BLOCKED says it must not proceed and shows the reason", () => {
    const h = r(<GovernanceView data={BLOCKED} loading={false} error={null} />);
    expect(h).toContain("BLOCKED");
    expect(h).toContain("should not proceed");
    expect(h).toContain("Confidence below threshold");
  });
  it("shows NO DATA when there is no verdict (nothing invented)", () => {
    const h = r(<GovernanceView data={EMPTY} loading={false} error={null} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("APPROVED");
  });
  it("shows LOADING while evaluating", () => {
    expect(r(<GovernanceView data={null} loading error={null} />)).toContain("LOADING");
  });
});

describe("governance fetch — same-origin proxy only", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("fetchGovernance hits /api/dashboard/ai-governance/{symbol}", async () => {
    vi.stubGlobal("fetch", okFetch(APPROVED));
    const res = await fetchGovernance("NVDA");
    expect(calls[0]).toBe("/api/dashboard/ai-governance/NVDA");
    expect(res.status).toBe("APPROVED");
    expect(res.approved).toBe(true);
  });
  it("fetchGovernanceFeed hits /api/dashboard/ai-governance", async () => {
    vi.stubGlobal("fetch", okFetch({ count: 1, decisions: [{ prediction_id: "NVDA:2026-08-16T15", status: "APPROVED" }], status_counts: { APPROVED: 1 } }));
    const res = await fetchGovernanceFeed();
    expect(calls[0]).toBe("/api/dashboard/ai-governance");
    expect(res.count).toBe(1);
    expect(res.status_counts.APPROVED).toBe(1);
  });
  it("rejects on a non-OK response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchGovernance("NVDA")).rejects.toBeTruthy();
  });
});
