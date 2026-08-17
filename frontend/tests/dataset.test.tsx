import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Datasets } from "@/components/Datasets";
import { datasetTone, shortChecksum, rangeText } from "@/lib/dataset";
import { fetchDatasets, fetchDataset, fetchDatasetCoverage, createDataset } from "@/lib/api";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("dataset helpers", () => {
  it("status → tone", () => {
    expect(datasetTone("COMPLETED")).toBe("ready");
    expect(datasetTone("RUNNING")).toBe("warning");
    expect(datasetTone("PLANNED")).toBe("warning");
    expect(datasetTone("FAILED")).toBe("blocked");
    expect(datasetTone(null)).toBe("nodata");
  });
  it("shortChecksum keeps the sha256 prefix and never fabricates when absent", () => {
    expect(shortChecksum("sha256:abcdef0123456789", 8)).toBe("sha256:abcdef01");
    expect(shortChecksum(null)).toBe("NO DATA");
  });
  it("rangeText renders NO DATA honestly when incomplete", () => {
    expect(rangeText({ range_start: "2023-01-03", range_end: "2023-06-30" })).toBe("2023-01-03 → 2023-06-30");
    expect(rangeText({ range_start: null, range_end: "2023-06-30" })).toBe("NO DATA");
  });
});

describe("Datasets page — research-data-only language, never live trading", () => {
  it("always shows RESEARCH DATA ONLY / IMMUTABLE / EXECUTION DISABLED and a build action", () => {
    const h = r(<Datasets connected />);
    expect(h).toContain("RESEARCH DATA ONLY");
    expect(h).toContain("IMMUTABLE");
    expect(h).toContain("EXECUTION");
    expect(h).toContain("DISABLED");
    expect(h).toContain("Build Dataset");
    expect(h).not.toMatch(/>\s*(Trade|Execute|Place Order|Buy|Sell)\s*</);
  });
  it("shows the disconnected banner + NO DATA when the backend is unreachable", () => {
    const h = r(<Datasets connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
  });
});

describe("dataset fetchers — same-origin proxy only (research read-only)", () => {
  let calls: { url: string; init?: any }[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string, init?: any) => { calls.push({ url: String(url), init }); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("read fetchers hit the right dataset sub-resources under /dashboard/research-datasets", async () => {
    vi.stubGlobal("fetch", okFetch({ count: 0, datasets: [], per_symbol: [] }));
    await fetchDatasets(); expect(calls[0].url).toBe("/api/dashboard/research-datasets");
    calls = []; await fetchDataset("D1"); expect(calls[0].url).toBe("/api/dashboard/research-datasets/D1");
    calls = []; await fetchDatasetCoverage("D1"); expect(calls[0].url).toBe("/api/dashboard/research-datasets/D1/coverage");
  });
  it("createDataset POSTs to /api/dashboard/research-datasets", async () => {
    vi.stubGlobal("fetch", okFetch({ dataset_id: "D1", status: "COMPLETED" }));
    const res = await createDataset({ symbols: ["NVDA"], interval: "1D", start: "2023-01-03", end: "2023-06-30" });
    expect(calls[0].url).toBe("/api/dashboard/research-datasets");
    expect(calls[0].init.method).toBe("POST");
    expect(res.ok).toBe(true);
    expect(res.data?.status).toBe("COMPLETED");
  });
  it("createDataset surfaces the 403 BACKFILL_DISABLED state honestly (never hidden)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: { detail: "BACKFILL_DISABLED", message: "backfill is disabled" } }) } as any)));
    const res = await createDataset({ symbols: ["NVDA"], interval: "1D", start: "2023-01-03", end: "2023-06-30" });
    expect(res.ok).toBe(false);
    expect(res.disabled).toBe(true);
    expect(res.detail).toContain("disabled");
  });
  it("createDataset surfaces a 422 bounds error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ detail: { error: "symbols not in the approved R3.0A universe" } }) } as any)));
    const res = await createDataset({ symbols: ["TSLA"], interval: "1D", start: "2023-01-03", end: "2023-06-30" });
    expect(res.ok).toBe(false);
    expect(res.detail).toContain("approved R3.0A universe");
  });
  it("read fetchers reject on non-OK (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchDatasets()).rejects.toBeTruthy();
  });
});

describe("proxy route whitelists research datasets and maps to /research/datasets", () => {
  const route = readFileSync(join(process.cwd(), "app", "api", "dashboard", "[...path]", "route.ts"), "utf8");
  it("research-datasets is in both READ and WRITE whitelists and maps to the backend research/datasets path", () => {
    expect(route).toContain("\"research-datasets\"");
    expect(route).toContain("research/datasets");
    // never a broker/order/execution path
    for (const forbidden of ["placeOrder", "cancelOrder", " orders", "execute", ":4002", "ib_insync"]) {
      expect(route.includes(forbidden), `proxy must not reference ${forbidden}`).toBe(false);
    }
  });
});
