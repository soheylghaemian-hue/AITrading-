import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DataQualityView } from "@/components/DataQualityPanel";
import { fetchCompleteness } from "@/lib/api";
import { hasCompleteness, readyForCapital, stateTone, type Completeness } from "@/lib/completeness";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

function dom(label: string, weight: number, score: number, available: boolean): Completeness["details"][string] {
  return { label, weight, score, available, checks: {} };
}

const PARTIAL: Completeness = {
  symbol: "NVDA", score: 58, state: "PARTIAL",
  available: ["market", "news", "fundamentals"], missing: ["options", "trader", "macro"], partial: ["technical"],
  details: {
    market: dom("Market Data", 20, 100, true), technical: dom("Technical", 15, 33.3, false),
    news: dom("News", 15, 100, true), fundamentals: dom("Fundamentals", 20, 100, true),
    options: dom("Options", 10, 0, false), trader: dom("Trader Intelligence", 10, 0, false),
    macro: dom("Macro", 10, 0, false),
  },
};
const READY: Completeness = {
  symbol: "NVDA", score: 90, state: "READY",
  available: ["market", "technical", "news", "fundamentals", "options", "trader"], missing: ["macro"], partial: [],
  details: { macro: dom("Macro", 10, 0, false) } as any,
};
const INSUF: Completeness = {
  symbol: "NVDA", score: 20, state: "INSUFFICIENT",
  available: [], missing: ["market", "technical", "news", "options", "trader", "macro"], partial: ["fundamentals"],
  details: { fundamentals: dom("Fundamentals", 20, 33.3, false) } as any,
};
const EMPTY: Completeness = { symbol: "NVDA", score: null, state: null, available: [], missing: [], partial: [], details: {} };

describe("completeness helpers", () => {
  it("detects a reading + maps state tone + capital readiness", () => {
    expect(hasCompleteness(PARTIAL)).toBe(true);
    expect(hasCompleteness(EMPTY)).toBe(false);
    expect(stateTone("READY")).toBe("ready");
    expect(stateTone("PARTIAL")).toBe("partial");
    expect(stateTone("INSUFFICIENT")).toBe("insufficient");
    expect(readyForCapital(READY)).toBe(true);
    expect(readyForCapital(PARTIAL)).toBe(false);
  });
});

describe("DataQualityView — reliability layer, never fabricated", () => {
  it("PARTIAL: shows score, state, available + missing, and the NOT-READY warning", () => {
    const h = r(<DataQualityView data={PARTIAL} loading={false} error={null} />);
    expect(h).toContain("Data Completeness");
    expect(h).toContain("58");
    expect(h).toContain("PARTIAL");
    expect(h).toContain("Market Data");            // available
    expect(h).toContain("Options");                // missing
    expect(h).toContain("Macro");
    expect(h).toContain("NO DATA");                // missing rendered as NO DATA
    expect(h).toContain("NOT READY FOR CAPITAL");
  });
  it("READY: no capital warning", () => {
    const h = r(<DataQualityView data={READY} loading={false} error={null} />);
    expect(h).toContain("READY");
    expect(h).not.toContain("NOT READY FOR CAPITAL");
  });
  it("INSUFFICIENT: shows the insufficient warning", () => {
    const h = r(<DataQualityView data={INSUF} loading={false} error={null} />);
    expect(h).toContain("INSUFFICIENT");
    expect(h).toContain("NOT READY FOR CAPITAL");
  });
  it("shows NO DATA when there is no reading (nothing invented)", () => {
    const h = r(<DataQualityView data={EMPTY} loading={false} error={null} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("PARTIAL");
  });
  it("shows LOADING while measuring", () => {
    expect(r(<DataQualityView data={null} loading error={null} />)).toContain("LOADING");
  });
});

describe("fetchCompleteness — same-origin proxy only", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/data-completeness/{symbol}", async () => {
    vi.stubGlobal("fetch", okFetch(PARTIAL));
    const res = await fetchCompleteness("NVDA");
    expect(calls[0]).toBe("/api/dashboard/data-completeness/NVDA");
    expect(res.state).toBe("PARTIAL");
    expect(res.missing).toContain("options");
  });
  it("rejects on a non-OK response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchCompleteness("NVDA")).rejects.toBeTruthy();
  });
});
