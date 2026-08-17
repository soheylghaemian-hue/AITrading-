import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { RiskCard } from "@/components/terminal/RiskCard";
import { validate } from "@/components/RiskConfigForm";
import { fetchRiskStatus, fetchRiskConfig, fetchRiskEvents, updateRiskConfig } from "@/lib/api";
import {
  stateTone, pctNum, usedFrac, moneyIn, reasonLabel, severityTone, eventTitle, hasRiskState,
  type RiskStatus,
} from "@/lib/risk";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const base = (over: Partial<RiskStatus> = {}): RiskStatus => ({
  status: "READY", reasons: [], missing: [],
  capital: { value: 100000, currency: "USD", source: "risk_config" },
  daily_pnl: { value: -300, limit: 2000, used_pct: 15, remaining: 1700, observed_at: "2026-08-17T10:00:00Z" },
  position_risk: { value: null, limit: 1 },
  exposure: { gross_pct: 40, net_pct: 20, limit_pct: 100 },
  drawdown: { value_pct: 3, limit_pct: 20 },
  kill_switch: "ARMED", configuration_version: 3, version_token: "abc123",
  updated_at: "2026-08-17T09:00:00Z", ts: "2026-08-17T10:00:00Z", ...over,
});

describe("risk helpers", () => {
  it("maps state → tone", () => {
    expect(stateTone("READY")).toBe("ready");
    expect(stateTone("WARNING")).toBe("warning");
    expect(stateTone("BLOCKED")).toBe("blocked");
    expect(stateTone("NO DATA")).toBe("nodata");
    expect(stateTone(null)).toBe("nodata");
  });
  it("pctNum keeps whole-number percents (never ×100), NO DATA when absent", () => {
    expect(pctNum(2)).toBe("2%");
    expect(pctNum(80)).toBe("80%");
    expect(pctNum(15.5)).toBe("15.5%");
    expect(pctNum(null)).toBe("NO DATA");
    expect(pctNum(NaN)).toBe("NO DATA");
  });
  it("usedFrac clamps 0..1, null when absent (never fabricated 0)", () => {
    expect(usedFrac(50)).toBeCloseTo(0.5);
    expect(usedFrac(150)).toBe(1);
    expect(usedFrac(null)).toBe(null);
  });
  it("moneyIn is currency-aware and NO DATA when missing (a real 0 shows)", () => {
    expect(moneyIn(1000, "USD")).toContain("1,000");
    expect(moneyIn(0, "USD")).toContain("0");        // observed zero is shown
    expect(moneyIn(null, "USD")).toBe("NO DATA");     // missing is NO DATA, not 0
    expect(moneyIn(500, "XYZ")).toContain("XYZ");      // unknown currency falls back gracefully
  });
  it("reasonLabel/severityTone/eventTitle translate codes", () => {
    expect(reasonLabel("DAILY_LOSS_LIMIT_EXCEEDED")).toMatch(/daily loss/i);
    expect(reasonLabel("KILL_SWITCH_TRIGGERED")).toMatch(/kill switch/i);
    expect(severityTone("CRITICAL")).toBe("blocked");
    expect(severityTone("WARNING")).toBe("warning");
    expect(severityTone("INFO")).toBe("ready");
    expect(eventTitle("CONFIGURATION_UPDATED")).toBe("Configuration updated");
    expect(eventTitle("KILL_SWITCH_TRIGGERED")).toBe("Kill switch engaged");
  });
  it("hasRiskState requires a real backend status", () => {
    expect(hasRiskState(base())).toBe(true);
    expect(hasRiskState(null)).toBe(false);
    expect(hasRiskState(base({ status: null }))).toBe(false);
  });
});

describe("RiskCard — compact terminal read-out (never fabricated)", () => {
  it("READY shows state + kill switch + budget, links to Risk Center", () => {
    const h = r(<RiskCard data={base()} />);
    expect(h).toContain("Risk Control");
    expect(h).toContain("READY");
    expect(h).toContain("ARMED");
    expect(h).toContain("15%");
    expect(h).toContain('href="/risk"');
  });
  it("WARNING surfaces reasons", () => {
    const h = r(<RiskCard data={base({ status: "WARNING", reasons: ["DAILY_LOSS_WARNING"] })} />);
    expect(h).toContain("WARNING");
    expect(h).toMatch(/approaching daily loss/i);
  });
  it("BLOCKED (kill switch) shows HALTED reason", () => {
    const h = r(<RiskCard data={base({ status: "BLOCKED", kill_switch: "STOPPED", reasons: ["KILL_SWITCH_TRIGGERED"] })} />);
    expect(h).toContain("BLOCKED");
    expect(h).toContain("STOPPED");
    expect(h).toMatch(/kill switch/i);
  });
  it("NO DATA renders without inventing numbers", () => {
    const h = r(<RiskCard data={null} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("%");
  });
});

describe("RiskConfigForm.validate — client mirror of backend bounds", () => {
  const good = {
    capital: "100000", currency: "USD", max_daily_loss_pct: "2", max_position_risk_pct: "1",
    max_portfolio_exposure_pct: "100", max_drawdown_pct: "20", warning_threshold_pct: "80",
  };
  it("accepts a valid config and parses numbers", () => {
    const { errors, parsed } = validate(good);
    expect(errors).toHaveLength(0);
    expect(parsed?.capital).toBe(100000);
    expect(parsed?.currency).toBe("USD");
  });
  it("rejects non-positive capital", () => {
    expect(validate({ ...good, capital: "0" }).errors.join()).toMatch(/capital/i);
    expect(validate({ ...good, capital: "" }).errors.join()).toMatch(/capital/i);
  });
  it("rejects an out-of-range warning threshold and unknown currency", () => {
    expect(validate({ ...good, warning_threshold_pct: "100" }).errors.join()).toMatch(/warning/i);
    expect(validate({ ...good, currency: "BTC" }).errors.join()).toMatch(/currency/i);
  });
  it("rejects a daily loss > 100%", () => {
    expect(validate({ ...good, max_daily_loss_pct: "150" }).errors.join()).toMatch(/daily loss/i);
  });
});

describe("risk fetch — same-origin proxy only (read-only), no token in browser", () => {
  let calls: { url: string; init?: any }[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string, init?: any) => { calls.push({ url: String(url), init }); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("fetchRiskStatus hits /api/dashboard/risk-status", async () => {
    vi.stubGlobal("fetch", okFetch(base()));
    const res = await fetchRiskStatus();
    expect(calls[0].url).toBe("/api/dashboard/risk-status");
    expect(res.status).toBe("READY");
  });
  it("fetchRiskConfig hits /api/dashboard/risk-config", async () => {
    vi.stubGlobal("fetch", okFetch({ configured: true, version_token: "t1", config: { capital: 100000 } }));
    const res = await fetchRiskConfig();
    expect(calls[0].url).toBe("/api/dashboard/risk-config");
    expect(res.configured).toBe(true);
  });
  it("fetchRiskEvents hits /api/dashboard/risk-events?limit=", async () => {
    vi.stubGlobal("fetch", okFetch({ count: 0, events: [] }));
    await fetchRiskEvents(50);
    expect(calls[0].url).toBe("/api/dashboard/risk-events?limit=50");
  });
  it("updateRiskConfig POSTs to /api/dashboard/risk-config with the expected_version", async () => {
    vi.stubGlobal("fetch", okFetch({ ok: true, configuration_version: 4 }));
    const res = await updateRiskConfig({
      capital: 100000, currency: "USD", max_daily_loss_pct: 2, max_position_risk_pct: 1,
      max_portfolio_exposure_pct: 100, max_drawdown_pct: 20, warning_threshold_pct: 80,
      expected_version: "abc123",
    });
    expect(calls[0].url).toBe("/api/dashboard/risk-config");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(calls[0].init.body).expected_version).toBe("abc123");
    expect(res.ok).toBe(true);
  });
  it("updateRiskConfig surfaces a 409 version conflict", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: "version conflict" }) } as any)));
    const res = await updateRiskConfig({
      capital: 100000, currency: "USD", max_daily_loss_pct: 2, max_position_risk_pct: 1,
      max_portfolio_exposure_pct: 100, max_drawdown_pct: 20, warning_threshold_pct: 80,
    });
    expect(res.ok).toBe(false);
    expect(res.conflict).toBe(true);
  });
  it("updateRiskConfig surfaces 422 validation errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ detail: { errors: ["Capital must be > 0"] } }) } as any)));
    const res = await updateRiskConfig({
      capital: -1, currency: "USD", max_daily_loss_pct: 2, max_position_risk_pct: 1,
      max_portfolio_exposure_pct: 100, max_drawdown_pct: 20, warning_threshold_pct: 80,
    });
    expect(res.ok).toBe(false);
    expect(res.errors).toContain("Capital must be > 0");
  });
  it("fetchRiskStatus rejects on non-OK (caller shows NO DATA, never fabricated)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchRiskStatus()).rejects.toBeTruthy();
  });
});
