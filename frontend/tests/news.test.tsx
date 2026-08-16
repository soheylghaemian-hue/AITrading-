import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NewsList } from "@/components/terminal/NewsFeed";
import { fetchNews } from "@/lib/api";
import { isValidNewsItem, sentimentTone, impactTone, type NewsItem } from "@/lib/news";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const NEWS: NewsItem[] = [
  { id: "1", symbol: "NVDA", title: "NVDA earnings beat, stock surges", source: "MarketWatch",
    url: "https://ex.com/a", published_at: "2026-08-16T10:00:00Z", summary: "strong",
    sentiment_score: 0.6, sentiment: "positive", impact: "HIGH" },
  { id: "2", symbol: "NVDA", title: "Regulator opens probe into chipmaker", source: "Reuters",
    url: null, published_at: "2026-08-16T09:00:00Z", summary: null,
    sentiment_score: -0.6, sentiment: "negative", impact: "MEDIUM" },
];

describe("news helpers", () => {
  it("validates real items and drops partial ones", () => {
    expect(isValidNewsItem(NEWS[0])).toBe(true);
    expect(isValidNewsItem({ title: "", published_at: "x" })).toBe(false);
    expect(isValidNewsItem({ title: "x" })).toBe(false);      // missing timestamp
    expect(isValidNewsItem(null)).toBe(false);
  });
  it("maps sentiment/impact to tone classes", () => {
    expect(sentimentTone("positive")).toBe("pos");
    expect(sentimentTone("negative")).toBe("neg");
    expect(sentimentTone(null)).toBe("neu");
    expect(impactTone("HIGH")).toBe("hi");
    expect(impactTone("MEDIUM")).toBe("med");
    expect(impactTone(null)).toBe("lo");
  });
});

describe("NewsList — presentational, never fabricates", () => {
  it("renders real headlines with source, sentiment and impact", () => {
    const h = r(<NewsList items={NEWS} loading={false} error={null} />);
    expect(h).toContain("NVDA earnings beat, stock surges");
    expect(h).toContain("Regulator opens probe into chipmaker");
    expect(h).toContain("MarketWatch");
    expect(h).toContain("Reuters");
    expect(h).toContain("POSITIVE");
    expect(h).toContain("NEGATIVE");
    expect(h).toContain("HIGH");
    expect(h).toContain('href="https://ex.com/a"');          // linked when a url exists
  });
  it("shows LOADING while fetching (no headlines yet)", () => {
    const h = r(<NewsList items={null} loading error={null} />);
    expect(h).toContain("LOADING");
    expect(h).not.toContain("earnings");
  });
  it("shows NO DATA on error (nothing invented)", () => {
    const h = r(<NewsList items={null} loading={false} error="unavailable" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("unavailable");
  });
  it("shows NO DATA for an empty / all-invalid feed (no fabricated news)", () => {
    expect(r(<NewsList items={[]} loading={false} error={null} />)).toContain("No news for this symbol");
    const bad = [{ title: "", published_at: "2026-08-16T10:00:00Z" }] as any;
    expect(r(<NewsList items={bad} loading={false} error={null} />)).toContain("NO DATA");
  });
});

describe("fetchNews — reads only through the same-origin proxy", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hits /api/dashboard/news/{symbol} and parses items", async () => {
    vi.stubGlobal("fetch", okFetch({ symbol: "NVDA", items: NEWS }));
    const res = await fetchNews("NVDA", 30);
    expect(calls[0]).toBe("/api/dashboard/news/NVDA?limit=30");
    expect(res.items.length).toBe(2);
  });
  it("symbol switching NVDA → AAPL fetches distinct proxy URLs", async () => {
    vi.stubGlobal("fetch", okFetch({ symbol: "X", items: [] }));
    for (const s of ["NVDA", "AAPL"]) await fetchNews(s, 30);
    expect(calls).toEqual(["/api/dashboard/news/NVDA?limit=30", "/api/dashboard/news/AAPL?limit=30"]);
  });
  it("rejects on a non-OK backend response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchNews("NVDA", 30)).rejects.toBeTruthy();
  });
  it("returns an empty feed (not an error) when the backend has no news yet", async () => {
    vi.stubGlobal("fetch", okFetch({ symbol: "NVDA", items: [] }));
    expect((await fetchNews("NVDA", 30)).items).toEqual([]);
  });
});
