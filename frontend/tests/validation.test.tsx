import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Validation } from "@/components/Validation";
import { fetchValidationCoverage, fetchValidationGate, fetchValidationRuns, fetchValidationRun, statusTone,
  validationGate, type ValidationRun } from "@/lib/validation";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("AI Validation view — research-data-only, never trading", () => {
  it("shows RESEARCH DATA ONLY / pilot / EXECUTION DISABLED, no trade controls", () => {
    const h = r(<Validation connected />);
    expect(h).toContain("RESEARCH DATA ONLY");
    expect(h).toContain("PILOT");
    expect(h).toContain("EXECUTION");
    expect(h).toContain("DISABLED");
    expect(h).not.toMatch(/>\s*(Trade|Execute|Place Order|Buy|Sell|Enable)\s*</);
  });
  it("renders INSUFFICIENT DATA and NOT APPLICABLE calibration when there is no run", () => {
    const h = r(<Validation connected />);
    expect(h).toContain("INSUFFICIENT DATA");
    expect(h).toContain("NOT APPLICABLE");
  });
  it("shows the disconnected NO DATA banner when the backend is unreachable", () => {
    const h = r(<Validation connected={false} />);
    expect(h.toLowerCase()).toContain("not reachable");
    expect(h).toContain("NO DATA");
  });
  it("statusTone maps INSUFFICIENT to nodata (never a trading-ready tone)", () => {
    expect(statusTone("INSUFFICIENT")).toBe("nodata");
    expect(statusTone("COMPLETED")).toBe("ready");
    expect(statusTone("FAILED")).toBe("blocked");
  });
});

describe("validation fetchers — same-origin GET-only research read models", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("hit /api/dashboard/research-validation/* (coverage, runs, run detail)", async () => {
    vi.stubGlobal("fetch", okFetch({ count: 0, runs: [], coverage: {}, universe: {} }));
    await fetchValidationCoverage(); expect(calls[0]).toBe("/api/dashboard/research-validation/coverage");
    calls = []; await fetchValidationRuns(); expect(calls[0]).toBe("/api/dashboard/research-validation/runs");
    calls = []; await fetchValidationRun("R1"); expect(calls[0]).toBe("/api/dashboard/research-validation/runs/R1");
  });
  it("reject on non-OK (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchValidationCoverage()).rejects.toBeTruthy();
  });
});

// § R3.1A.2 — the gate that any positive verdict (incl. the legacy AI Performance panel) depends on.
describe("validationGate — fail-closed derivation from the runs page", () => {
  const run = (o: Partial<ValidationRun>): ValidationRun => ({
    run_id: "R", status: "COMPLETED", gate_id: "G1", result_checksum: "c", commit_sha: "a".repeat(40),
    created_at: "2026-08-01T00:00:00Z", ended_at: "2026-08-01T00:01:00Z", ...o,
  });

  it("validates only on the latest COMPLETED run with gate_passed true", () => {
    const g = validationGate([run({ run_id: "R2", gate_passed: true, created_at: "2026-08-02T00:00:00Z" }),
      run({ run_id: "R1", gate_passed: false })]);
    expect(g).toMatchObject({ validated: true, reason: "VALIDATED", run_id: "R2" });
  });
  it("uses the newest COMPLETED run even when the page arrives out of order", () => {
    const g = validationGate([run({ run_id: "OLD", gate_passed: true, created_at: "2026-07-01T00:00:00Z" }),
      run({ run_id: "NEW", gate_passed: false, created_at: "2026-08-09T00:00:00Z" })]);
    expect(g).toMatchObject({ validated: false, reason: "GATE_NOT_PASSED", run_id: "NEW" });
  });
  it("is NOT validated with no runs, only INSUFFICIENT/RUNNING/FAILED runs, or a missing gate report", () => {
    expect(validationGate([])).toMatchObject({ validated: false, reason: "NO_COMPLETED_RUN" });
    expect(validationGate(null)).toMatchObject({ validated: false, reason: "NO_COMPLETED_RUN" });
    for (const s of ["INSUFFICIENT", "RUNNING", "FAILED"])
      expect(validationGate([run({ status: s, gate_passed: true })]))
        .toMatchObject({ validated: false, reason: "NO_COMPLETED_RUN", status: s });
    expect(validationGate([run({ gate_passed: null })]))
      .toMatchObject({ validated: false, reason: "GATE_NOT_PASSED" });
    expect(validationGate([run({})])).toMatchObject({ validated: false, reason: "GATE_NOT_PASSED" });
  });
  it("falls back to the detail gate_report.passed when the summary omits gate_passed", () => {
    expect(validationGate([run({ gate_report: { passed: true, criteria: {} } })]))
      .toMatchObject({ validated: true });
    expect(validationGate([run({ gate_report: { passed: false, criteria: {} } })]))
      .toMatchObject({ validated: false, reason: "GATE_NOT_PASSED" });
  });

  afterEach(() => { vi.unstubAllGlobals(); });
  it("fetchValidationGate never rejects — an unreachable backend is NOT VALIDATED", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchValidationGate()).resolves.toMatchObject({ validated: false, reason: "UNAVAILABLE" });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network down"); }));
    await expect(fetchValidationGate()).resolves.toMatchObject({ validated: false, reason: "UNAVAILABLE" });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200,
      json: async () => ({ count: 1, runs: [run({ gate_passed: true })] }) } as any)));
    await expect(fetchValidationGate()).resolves.toMatchObject({ validated: true });
  });
});

describe("proxy whitelists research-validation/intel as GET-only, no runner POST", () => {
  const route = readFileSync(join(process.cwd(), "app", "api", "dashboard", "[...path]", "route.ts"), "utf8");
  it("research-validation + research-intel are READ paths mapping to /research/validation and /research/intel", () => {
    expect(route).toContain("\"research-validation\"");
    expect(route).toContain("\"research-intel\"");
    expect(route).toContain("research/validation");
    expect(route).toContain("research/intel");
    // they are NOT in WRITE_PATHS (no POST that runs collection/evaluation/validation)
    const write = route.slice(route.indexOf("WRITE_PATHS"));
    expect(write.slice(0, 200)).not.toContain("research-validation");
    expect(write.slice(0, 200)).not.toContain("research-intel");
  });
});
