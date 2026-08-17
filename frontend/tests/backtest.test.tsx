import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Backtesting } from "@/components/Backtesting";
import { BacktestForm } from "@/components/BacktestForm";
import { statusTone, metricText, pctText } from "@/lib/backtest";
import {
  createBacktest, fetchBacktests, fetchBacktest, fetchBacktestMetrics, fetchBacktestTrades,
  fetchBacktestEquity, fetchBacktestEvents,
} from "@/lib/api";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("backtest helpers", () => {
  it("status → tone", () => {
    expect(statusTone("COMPLETED")).toBe("ready");
    expect(statusTone("RUNNING")).toBe("warning");
    expect(statusTone("FAILED")).toBe("blocked");
    expect(statusTone(null)).toBe("nodata");
  });
  it("metricText passes through NO DATA / NOT APPLICABLE honestly, never fabricates", () => {
    expect(metricText("NO DATA")).toBe("NO DATA");
    expect(metricText("NOT APPLICABLE")).toBe("NOT APPLICABLE");
    expect(metricText(null)).toBe("NO DATA");
    expect(metricText(1.234, 2)).toBe("1.23");
    expect(pctText(0.0919)).toBe("9.19%");
    expect(pctText("NOT APPLICABLE")).toBe("NOT APPLICABLE");
  });
});

describe("Backtesting page — research-only language, never live trading", () => {
  it("always shows RESEARCH ONLY / NOT LIVE TRADING / EXECUTION DISABLED and a 'Run Backtest' action", () => {
    const h = r(<Backtesting connected />);
    expect(h).toContain("RESEARCH ONLY");
    expect(h).toContain("NOT LIVE TRADING");
    expect(h).toContain("EXECUTION");
    expect(h).toContain("DISABLED");
    expect(h).toContain("Run Backtest");
    // never a live-trading control label
    expect(h).not.toMatch(/>\s*(Trade|Execute|Place Order|Buy|Sell)\s*</);
  });
  it("shows the disconnected banner + NO DATA when the backend is unreachable", () => {
    const h = r(<Backtesting connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
  });
  it("the form's primary action is 'Run Backtest' and carries the research-only disclaimer", () => {
    const h = r(<BacktestForm connected onRun={() => {}} />);
    expect(h).toContain("Run Backtest");
    expect(h.toLowerCase()).toContain("never trades, never places an order");
    expect(h).toContain("OHLC_TREND_BASELINE");
  });
});

describe("backtest fetchers — same-origin proxy only (research read-only)", () => {
  let calls: { url: string; init?: any }[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string, init?: any) => { calls.push({ url: String(url), init }); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("createBacktest POSTs to /api/dashboard/backtests", async () => {
    vi.stubGlobal("fetch", okFetch({ run_id: "r1", status: "COMPLETED" }));
    const res = await createBacktest({ symbols: ["NVDA"], interval: "1D", start: "2026-01-05", end: "2026-06-30" });
    expect(calls[0].url).toBe("/api/dashboard/backtests");
    expect(calls[0].init.method).toBe("POST");
    expect(res.ok).toBe(true);
    expect(res.data?.status).toBe("COMPLETED");
  });
  it("createBacktest surfaces 422 validation errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ detail: { errors: ["unsupported interval '5m'"] } }) } as any)));
    const res = await createBacktest({ symbols: ["NVDA"], interval: "5m", start: "a", end: "b" });
    expect(res.ok).toBe(false);
    expect(res.errors).toContain("unsupported interval '5m'");
  });
  it("createBacktest surfaces the 409 one-active-run conflict", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: "ONE_ACTIVE_RUN" }) } as any)));
    const res = await createBacktest({ symbols: ["NVDA"], interval: "1D", start: "a", end: "b" });
    expect(res.ok).toBe(false);
    expect(res.conflict).toBe(true);
  });
  it("read fetchers hit the right run sub-resources", async () => {
    vi.stubGlobal("fetch", okFetch({ count: 0, runs: [], trades: [], equity: [], events: [], metrics: {} }));
    await fetchBacktests(); expect(calls[0].url).toBe("/api/dashboard/backtests");
    calls = []; await fetchBacktest("R"); expect(calls[0].url).toBe("/api/dashboard/backtests/R");
    calls = []; await fetchBacktestMetrics("R"); expect(calls[0].url).toBe("/api/dashboard/backtests/R/metrics");
    calls = []; await fetchBacktestTrades("R"); expect(calls[0].url).toBe("/api/dashboard/backtests/R/trades");
    calls = []; await fetchBacktestEquity("R"); expect(calls[0].url).toBe("/api/dashboard/backtests/R/equity");
    calls = []; await fetchBacktestEvents("R"); expect(calls[0].url).toBe("/api/dashboard/backtests/R/events");
  });
  it("read fetchers reject on non-OK (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchBacktests()).rejects.toBeTruthy();
  });
});
