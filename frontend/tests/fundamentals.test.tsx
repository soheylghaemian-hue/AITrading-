import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { FundamentalsList } from "@/components/terminal/FundamentalsFeed";
import { fetchFundamentals } from "@/lib/api";
import { big, hasFundamentals, pct, qualityTier, valuationLabel, type FundamentalsData } from "@/lib/fundamentals";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const FUND: FundamentalsData = {
  symbol: "NVDA",
  company: { company_name: "NVIDIA Corporation", sector: "SEMICONDUCTORS", industry: "SEMICONDUCTORS", exchange: "XNAS", country: "US" },
  quality_score: 87,
  quality_breakdown: { growth: 95, profitability: 92, valuation: 64 },
  financials: { period: "FY", revenue: 130e9, revenue_growth: 0.35, gross_margin: 0.60, operating_margin: 0.50, net_margin: 0.44, eps: 2.3, eps_growth: 0.35, free_cash_flow: null, debt: null, cash: null },
  valuation: { market_cap: 3e12, pe_ratio: 50, forward_pe: null, price_sales: 23, enterprise_value: null },
  analyst_estimates: null,
  strengths: ["Revenue growth", "High margins"],
  risks: ["High valuation"],
};

const EMPTY: FundamentalsData = {
  symbol: "NVDA", company: null, quality_score: null, quality_breakdown: null, financials: null,
  valuation: null, analyst_estimates: null, strengths: [], risks: [],
};

describe("fundamentals helpers", () => {
  it("formats quality tier, percent, money and valuation label", () => {
    expect(qualityTier(87)).toBe("hi");
    expect(qualityTier(60)).toBe("med");
    expect(qualityTier(30)).toBe("lo");
    expect(qualityTier(null)).toBe("lo");
    expect(pct(0.35)).toBe("35.0%");
    expect(pct(null)).toBe("NO DATA");
    expect(big(3e12)).toBe("$3.00T");
    expect(big(null)).toBe("NO DATA");
    expect(valuationLabel(50)).toBe("High");
    expect(valuationLabel(12)).toBe("Low");
    expect(valuationLabel(null)).toBe("NO DATA");
    expect(hasFundamentals(EMPTY)).toBe(false);
    expect(hasFundamentals(FUND)).toBe(true);
  });
});

describe("FundamentalsList — real metrics, never fabricated", () => {
  it("renders quality, growth, profitability, valuation, strengths and risks", () => {
    const h = r(<FundamentalsList data={FUND} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NVIDIA Corporation");
    expect(h).toContain("87");                     // company quality score
    expect(h).toContain("35.0%");                  // revenue growth
    expect(h).toContain("44.0%");                  // net margin
    expect(h).toContain("50.0");                   // P/E
    expect(h).toContain("$3.00T");                 // market cap
    expect(h).toContain("High");                   // valuation label
    expect(h).toContain("Revenue growth");         // strength
    expect(h).toContain("High margins");
    expect(h).toContain("High valuation");         // risk
    expect(h).toContain("NO DATA");                // analyst estimates unavailable
  });
  it("shows LOADING while fetching (no metrics yet)", () => {
    const h = r(<FundamentalsList data={null} loading error={null} symbol="NVDA" />);
    expect(h).toContain("LOADING");
    expect(h).not.toContain("NVIDIA");
  });
  it("shows NO DATA on error (nothing invented)", () => {
    const h = r(<FundamentalsList data={null} loading={false} error="unavailable" symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("unavailable");
  });
  it("shows NO DATA when there is no coverage (no fabricated financials)", () => {
    const h = r(<FundamentalsList data={EMPTY} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("No fundamentals coverage");
    expect(h).not.toContain("NVIDIA");
  });
});

describe("fetchFundamentals — reads only through the same-origin proxy", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/fundamentals/{symbol} and parses the data", async () => {
    vi.stubGlobal("fetch", okFetch(FUND));
    const res = await fetchFundamentals("NVDA");
    expect(calls[0]).toBe("/api/dashboard/fundamentals/NVDA");
    expect(res.quality_score).toBe(87);
    expect(res.strengths.length).toBe(2);
  });
  it("symbol switching NVDA → AAPL fetches distinct proxy URLs", async () => {
    vi.stubGlobal("fetch", okFetch(EMPTY));
    for (const s of ["NVDA", "AAPL"]) await fetchFundamentals(s);
    expect(calls).toEqual(["/api/dashboard/fundamentals/NVDA", "/api/dashboard/fundamentals/AAPL"]);
  });
  it("rejects on a non-OK backend response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchFundamentals("NVDA")).rejects.toBeTruthy();
  });
});
