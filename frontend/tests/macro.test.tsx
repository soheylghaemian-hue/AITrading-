import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MacroView } from "@/components/MacroPanel";
import { MacroContextCard } from "@/components/terminal/MacroContextCard";
import { fetchMacro, fetchMacroContext } from "@/lib/api";
import { hasMacro, regimeLabel, regimeTone, relevanceTone, type Macro, type MacroContext } from "@/lib/macro";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const MACRO: Macro = {
  score: 72, regime: "RISK_NEUTRAL", status: "COMPLETE",
  signals: ["Inflation improving", "Volatility decreasing"], risks: ["Rates elevated"],
  metrics: {
    fed_rate: { label: "Fed Funds Rate", value: 4.5, trend: "flat" },
    treasury_10y: { label: "10Y Treasury", value: 4.1, trend: "down" },
    vix: { label: "VIX", value: 15.2, trend: "down" },
    dxy: { label: "US Dollar Index", value: 103, trend: "up" },
  },
  timestamp: "2026-08-16T12:00:00Z", source: "fred",
};
const EMPTY_MACRO: Macro = { score: null, regime: null, status: "NO DATA", signals: [], risks: [], metrics: {}, timestamp: null, source: null };

const CTX: MacroContext = {
  symbol: "NVDA", regime: "RISK_ON", score: 78, relevance: "TAILWIND",
  note: "Risk-supportive macro — a tailwind for equities/risk assets.", status: "COMPLETE",
  signals: ["Low volatility regime"], risks: [],
};
const EMPTY_CTX: MacroContext = { symbol: "NVDA", regime: null, score: null, relevance: null, note: null, status: "NO DATA", signals: [], risks: [] };

describe("macro helpers", () => {
  it("detects a reading + maps regime/relevance tone", () => {
    expect(hasMacro(MACRO)).toBe(true);
    expect(hasMacro(EMPTY_MACRO)).toBe(false);
    expect(regimeTone("RISK_ON")).toBe("on");
    expect(regimeTone("RISK_NEUTRAL")).toBe("neutral");
    expect(regimeTone("RISK_OFF")).toBe("off");
    expect(regimeLabel("RISK_NEUTRAL")).toBe("RISK NEUTRAL");
    expect(relevanceTone("TAILWIND")).toBe("on");
    expect(relevanceTone("HEADWIND")).toBe("off");
  });
});

describe("MacroView — global environment, never fabricated", () => {
  it("renders score, regime, metrics, drivers and risks", () => {
    const h = r(<MacroView data={MACRO} loading={false} error={null} />);
    expect(h).toContain("Macro Environment");
    expect(h).toContain("72");
    expect(h).toContain("RISK NEUTRAL");
    expect(h).toContain("VIX");
    expect(h).toContain("Inflation improving");     // driver
    expect(h).toContain("Rates elevated");          // risk
  });
  it("shows NO DATA with no snapshot (nothing invented)", () => {
    const h = r(<MacroView data={EMPTY_MACRO} loading={false} error={null} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("RISK NEUTRAL");
  });
  it("shows LOADING while reading", () => {
    expect(r(<MacroView data={null} loading error={null} />)).toContain("LOADING");
  });
});

describe("MacroContextCard — per-symbol relevance", () => {
  it("renders regime, relevance and note", () => {
    const h = r(<MacroContextCard data={CTX} />);
    expect(h).toContain("Macro Context");
    expect(h).toContain("RISK ON");
    expect(h).toContain("TAILWIND");
    expect(h).toContain("tailwind for equities");
  });
  it("shows NO DATA state", () => {
    const h = r(<MacroContextCard data={EMPTY_CTX} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("TAILWIND");
  });
});

describe("macro fetch — same-origin proxy only", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("fetchMacro hits /api/dashboard/macro", async () => {
    vi.stubGlobal("fetch", okFetch(MACRO));
    const res = await fetchMacro();
    expect(calls[0]).toBe("/api/dashboard/macro");
    expect(res.regime).toBe("RISK_NEUTRAL");
    expect(res.score).toBe(72);
  });
  it("fetchMacroContext hits /api/dashboard/macro-context/{symbol}", async () => {
    vi.stubGlobal("fetch", okFetch(CTX));
    const res = await fetchMacroContext("NVDA");
    expect(calls[0]).toBe("/api/dashboard/macro-context/NVDA");
    expect(res.relevance).toBe("TAILWIND");
  });
  it("rejects on a non-OK response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchMacro()).rejects.toBeTruthy();
  });
});
