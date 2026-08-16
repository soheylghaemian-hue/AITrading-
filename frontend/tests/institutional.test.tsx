import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { InstitutionalPanel } from "@/components/terminal/InstitutionalPanel";
import { fetchInstitutionalFlow } from "@/lib/api";
import { flowTone, fmtShares, hasInstitutional, insiderTone, type InstitutionalFlow } from "@/lib/institutional";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const FLOW: InstitutionalFlow = {
  symbol: "NVDA", status: "COMPLETE",
  institutional_changes: [
    { institution: "RENAISSANCE TECHNOLOGIES", symbol: "NVDA", previous_shares: 2526948, current_shares: 7091256, share_change: 4564308, percentage_change: 180.6, direction: "ACCUMULATION", filing_period: "2026-08-13" },
    { institution: "TIGER GLOBAL", symbol: "NVDA", previous_shares: 12011752, current_shares: 11198773, share_change: -812979, percentage_change: -6.8, direction: "REDUCTION", filing_period: "2026-08-14" },
  ],
  institutional_direction: "MIXED", accumulation_score: 50, net_share_change_pct: 15.8,
  insider_activity: [
    { insider_name: "STEVENS MARK A", title: "Director", transaction_type: "SELL", shares: 565615, price: 210.44, transaction_date: "2026-06-18" },
  ],
  insider_sentiment: "BEARISH", insider_score: 0,
  insider_summary: { buy_count: 0, sell_count: 43, buy_shares: 0, sell_shares: 2512857, distinct_buyers: 0 },
};
const EMPTY: InstitutionalFlow = {
  symbol: "NVDA", status: "NO DATA", institutional_changes: [], institutional_direction: null,
  accumulation_score: null, net_share_change_pct: null, insider_activity: [], insider_sentiment: null,
  insider_score: null, insider_summary: { buy_count: 0, sell_count: 0, buy_shares: 0, sell_shares: 0, distinct_buyers: 0 },
};

describe("institutional helpers", () => {
  it("detects data + maps tone + formats shares", () => {
    expect(hasInstitutional(FLOW)).toBe(true);
    expect(hasInstitutional(EMPTY)).toBe(false);
    expect(flowTone("ACCUMULATION")).toBe("acc");
    expect(flowTone("REDUCTION")).toBe("red");
    expect(flowTone("MIXED")).toBe("mixed");
    expect(insiderTone("BULLISH")).toBe("acc");
    expect(insiderTone("BEARISH")).toBe("red");
    expect(fmtShares(7091256)).toBe("7.1M");
    expect(fmtShares(null)).toBe("—");
  });
});

describe("InstitutionalPanel — smart money, never fabricated", () => {
  it("renders 13F changes + insider sentiment", () => {
    const h = r(<InstitutionalPanel data={FLOW} />);
    expect(h).toContain("Smart Money Flow");
    expect(h).toContain("MIXED");                        // institutional direction
    expect(h).toContain("+15.8%");                       // net change
    expect(h).toContain("RENAISSANCE TECHNOLOGIES");
    expect(h).toContain("+180.6%");                      // accumulation
    expect(h).toContain("BEARISH");                      // insider sentiment
    expect(h).toContain("STEVENS MARK A");
  });
  it("shows NO DATA when empty (nothing invented)", () => {
    const h = r(<InstitutionalPanel data={EMPTY} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("MIXED");
  });
  it("shows NO DATA for null", () => {
    expect(r(<InstitutionalPanel data={null} />)).toContain("NO DATA");
  });
});

describe("fetchInstitutionalFlow — same-origin proxy only", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/institutional-flow/{symbol}", async () => {
    vi.stubGlobal("fetch", okFetch(FLOW));
    const res = await fetchInstitutionalFlow("NVDA");
    expect(calls[0]).toBe("/api/dashboard/institutional-flow/NVDA");
    expect(res.institutional_direction).toBe("MIXED");
    expect(res.insider_sentiment).toBe("BEARISH");
  });
  it("rejects on a non-OK response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchInstitutionalFlow("NVDA")).rejects.toBeTruthy();
  });
});
