import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketTerminal } from "@/components/terminal/MarketTerminal";
import { statusStrip } from "@/lib/select";
import { computeReadiness } from "@/lib/readiness";
import { CandleChart } from "@/components/CandleChart";
import { AiAnalysisPanel } from "@/components/terminal/AiAnalysisPanel";
import { CapitalReadiness } from "@/components/CapitalReadiness";
import {
  GovernanceCard, CompletenessCard, NewsCard, FundamentalsCard, OptionsCard, TradersCard,
  InstitutionalCard, InsiderClusterCard,
} from "@/components/terminal/IntelCards";
import type { OhlcBar } from "@/lib/ohlc";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);
const CSS = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");
/** Extract the declaration block of a single CSS rule by its exact selector prefix. */
function ruleBlock(css: string, selector: string): string {
  const i = css.indexOf(selector + "{");
  if (i < 0) return "";
  return css.slice(i, css.indexOf("}", i));
}

// ---- UX-1.1 workbench layout — no dead space, independent columns ----------
describe("UX-1.1 workbench — chart never inherits the AI panel height", () => {
  const base: any = {
    mode: "paper", connected: true, execution_enabled: false,
    global_market_data: [{ region: "USA", symbol: "NVDA", source: "MASSIVE", status: "DATA_AVAILABLE", realtime: true, bid: 100.4, ask: 100.6, last: 100.5, volume: 1, latency_ms: 72, timestamp: "2026-08-15T14:30:00Z" }],
    autonomous: { status: "ARMED", decisions: [] },
  };
  it("flat DOM order is chart → AI → intel → detail (so mobile stacks in semantic order)", () => {
    const h = r(<MarketTerminal s={base} symbol="NVDA" connected />);
    const iChart = h.indexOf("wb-chart"), iAi = h.indexOf("wb-ai"), iIntel = h.indexOf("wb-intel"), iDetail = h.indexOf("wb-detail");
    expect(iChart).toBeGreaterThan(-1);
    expect(iAi).toBeGreaterThan(iChart);
    expect(iIntel).toBeGreaterThan(iAi);
    expect(iDetail).toBeGreaterThan(iIntel);
  });
  it("intelligence grid follows the chart inside the workbench (left-column structure)", () => {
    const h = r(<MarketTerminal s={base} symbol="NVDA" connected />);
    expect(h).toContain("intel-grid wb-intel");
    expect(h.indexOf("wb-chart")).toBeLessThan(h.indexOf("intel-grid wb-intel"));
  });
  it("workbench uses align-items:start (no grid stretch) and grid-areas — the buggy rule is gone", () => {
    const wb = ruleBlock(CSS, ".workbench");
    expect(wb).toContain("align-items:start");
    expect(wb).toContain("grid-template-areas");
    expect(CSS).not.toContain(".term-main{");   // the old single-row dead-space rule is removed
  });
  it("AI panel has a bounded desktop height with internal scroll", () => {
    const ai = ruleBlock(CSS, ".wb-ai");
    expect(ai).toContain("max-height:min(");
    expect(ai).toContain("overflow-y:auto");
    expect(ai).toContain("position:sticky");
  });
  it("mobile drops the desktop bound: single column, no forced height, no nested scroll", () => {
    // the ≤1000px media query relaxes the AI panel back to natural flow
    expect(CSS).toMatch(/@media \(max-width:1000px\)\{[^}]*\.workbench\{grid-template-columns:1fr/);
    expect(CSS).toMatch(/\.wb-ai\{position:static;max-height:none;overflow:visible/);
  });
  it("no excessive min-height / stretch on the workbench or chart shell", () => {
    for (const sel of [".workbench", ".wb-chart", ".term-chart", ".wb-intel", ".wb-detail"]) {
      const b = ruleBlock(CSS, sel);
      expect(b).not.toMatch(/min-height:\s*(100%|100vh)/);
      expect(b).not.toMatch(/flex:\s*1\b/);
      expect(b).not.toMatch(/align-items:\s*stretch/);
    }
  });
});

// ---- global safety strip (kill switch + execution always explicit) --------
describe("global safety strip — execution + kill switch always explicit, never fabricated", () => {
  const snap: any = { mode: "paper", connected: true, execution_enabled: false, system_status: "READY", global_market_data: [] };
  it("shows EXECUTION DISABLED and a KILL SWITCH pill when connected", () => {
    const pills = statusStrip(snap, true, "ARMED");
    const exec = pills.find((p) => p.key === "execution")!;
    const kill = pills.find((p) => p.key === "kill")!;
    expect(exec.value).toBe("DISABLED");
    expect(kill.value).toBe("ARMED");
    expect(kill.tone).toBe("g");
  });
  it("kill switch STOPPED is red; unknown is NO DATA grey", () => {
    expect(statusStrip(snap, true, "STOPPED").find((p) => p.key === "kill")!.tone).toBe("r");
    expect(statusStrip(snap, true, null).find((p) => p.key === "kill")!.value).toBe("NO DATA");
  });
  it("disconnected → EXECUTION DISABLED still shown and kill switch NO DATA (nothing invented)", () => {
    const pills = statusStrip(null, false);
    expect(pills.find((p) => p.key === "execution")!.value).toBe("DISABLED");
    expect(pills.find((p) => p.key === "kill")!.value).toBe("NO DATA");
    expect(pills.find((p) => p.key === "mode")!.value).toBe("NO DATA");
  });
  it("never reports LIVE/ENABLED unless the authoritative field says so", () => {
    const live = statusStrip({ ...snap, mode: "LIVE", execution_enabled: true }, true, "ARMED");
    expect(live.find((p) => p.key === "mode")!.value).toBe("LIVE");
    expect(live.find((p) => p.key === "execution")!.value).toBe("ENABLED");
    // paper stays visually distinct (teal) vs live (red)
    expect(statusStrip(snap, true).find((p) => p.key === "mode")!.tone).toBe("t");
  });
});

// ---- capital readiness — NOT READY, never inferred from absent positions ----
describe("capital readiness — honest, never a false READY, missing never zero", () => {
  const prodSnap: any = { connected: true, execution_enabled: false, account: null, positions: [] };
  it("production (no config, no risk data, execution disabled) → NOT READY with reasons", () => {
    const rd = computeReadiness({
      snapshot: prodSnap, connected: true,
      riskConfig: { configured: false, reason: "RISK_CONFIGURATION_MISSING", configuration_version: null, version_token: null, kill_switch: "ARMED", config: null },
      riskStatus: { status: "NO DATA", reasons: ["RISK_CONFIGURATION_MISSING"], missing: ["capital", "exposure", "drawdown"], capital: { value: null, currency: null, source: null }, daily_pnl: {} as any, position_risk: {} as any, exposure: {} as any, drawdown: {} as any, kill_switch: "ARMED", configuration_version: null, version_token: null, updated_at: null, ts: null },
    });
    expect(rd.label).toBe("NOT READY");
    expect(rd.ready).toBe(false);
    expect(rd.reasons.join(" ")).toMatch(/execution/i);
    const riskData = rd.checks.find((c) => c.key === "risk_data")!;
    expect(riskData.detail).toContain("exposure");     // missing shown by name…
    expect(riskData.detail).not.toMatch(/\b0\b/);       // …never as zero
    const broker = rd.checks.find((c) => c.key === "broker")!;
    expect(broker.detail).not.toContain("$0");          // missing equity/portfolio never $0
  });
  it("READY is never inferred from an empty portfolio", () => {
    const rd = computeReadiness({ snapshot: { connected: true, execution_enabled: false, positions: [] } as any, connected: true });
    expect(rd.label).not.toBe("READY");
  });
  it("fully disconnected → NO DATA (not READY, not fabricated)", () => {
    const rd = computeReadiness({ snapshot: null, connected: false });
    expect(rd.label).toBe("NO DATA");
    expect(r(<CapitalReadiness readiness={rd} />)).toContain("Capital Readiness");
  });
  it("renders the NOT READY verdict and the disclaimer", () => {
    const rd = computeReadiness({ snapshot: prodSnap, connected: true });
    const h = r(<CapitalReadiness readiness={rd} />);
    expect(h).toContain("NOT READY");
    expect(h.toLowerCase()).toContain("never enables trading");
  });
});

// ---- compact chart preserves all panels at a reduced height ----------------
describe("compact chart — smaller viewBox, all indicators preserved, no fabrication", () => {
  const bars: OhlcBar[] = Array.from({ length: 30 }, (_, i) => ({
    timestamp: `2026-08-15T14:${String(i).padStart(2, "0")}:00Z`, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i, volume: 1000 + i,
  }));
  it("compact reduces the viewBox height but keeps VOL / RSI / MACD panels", () => {
    const full = r(<CandleChart bars={bars} />);
    const comp = r(<CandleChart bars={bars} compact />);
    expect(full).toContain("0 0 1040 560");
    expect(comp).toContain("0 0 1040 432");
    for (const panel of ["VOL", "RSI 14", "MACD 12/26/9"]) expect(comp).toContain(panel);
  });
  it("still renders nothing for <2 real bars (never fabricated)", () => {
    expect(r(<CandleChart bars={[bars[0]]} compact />)).toBe("");
  });
});

// ---- AI explanation — drivers/risks/conflicts/missing; never simplified to BUY ----
describe("AI explanation — surfaces disagreement, never collapses to a single verdict", () => {
  const consensus: any = { symbol: "NVDA", score: 74, direction: "BULLISH", confidence: 68, status: "PARTIAL", coverage: 0.7, components: [], strengths: ["Strong momentum with expanding volume"], risks: ["Overbought on RSI"], conflicts: ["Fundamentals bullish vs Options bearish"] };
  const governance: any = { symbol: "NVDA", status: "PARTIAL", score: 74, confidence: 68, data_completeness: 60, reasons: ["MISSING_NEWS"], approved: false, direction: "BULLISH", missing: ["News"], conflicts: [] };
  it("shows direction, drivers, risks, conflicts, missing, governance, risk, completeness, execution", () => {
    const h = r(<AiAnalysisPanel dec={null} risk={null} mode="paper" executionEnabled={false}
      consensus={consensus} governance={governance} riskStatus="NO DATA" completeness={{ score: 60 } as any} />);
    expect(h).toContain("Strong momentum with expanding volume");   // driver
    expect(h).toContain("Overbought on RSI");                        // risk
    expect(h).toContain("Fundamentals bullish vs Options bearish");  // conflict, not hidden
    expect(h).toContain("News");                                     // missing input
    expect(h).toContain("PARTIAL");                                  // governance
    expect(h).toContain("NO DATA");                                  // risk status
    expect(h).toContain("60/100");                                   // completeness
    expect(h).toContain("DISABLED");                                 // execution
  });
  it("is all NO DATA when no assessment context is supplied", () => {
    const h = r(<AiAnalysisPanel dec={null} risk={null} mode="paper" executionEnabled={false} />);
    expect(h).toContain("NO DATA");
    expect(h).not.toContain("APPROVED");
  });
});

// ---- compact intelligence cards — populated + honest NO DATA ----------------
describe("compact intelligence cards — each shows real fields or NO DATA", () => {
  it("Governance card: status + NO DATA", () => {
    expect(r(<GovernanceCard data={null} />)).toContain("NO DATA");
    expect(r(<GovernanceCard data={{ symbol: "NVDA", status: "BLOCKED", score: 20, confidence: 30, data_completeness: 40, reasons: ["INSUFFICIENT_DATA"], approved: false, direction: null, missing: [], conflicts: [] }} />)).toContain("BLOCKED");
  });
  it("Data Completeness card: state + NO DATA", () => {
    expect(r(<CompletenessCard data={null} />)).toContain("NO DATA");
    expect(r(<CompletenessCard data={{ symbol: "NVDA", score: 72, state: "PARTIAL", available: ["fundamentals"], missing: ["news"], partial: [], details: {} }} />)).toContain("PARTIAL");
  });
  it("News card: count + NO DATA", () => {
    expect(r(<NewsCard items={null} />)).toContain("NO DATA");
    expect(r(<NewsCard items={[{ id: "1", symbol: "NVDA", title: "NVIDIA beats estimates", source: "Reuters", url: null, published_at: "2026-08-16T14:30:00Z", summary: null, sentiment_score: 0.5, sentiment: "positive", impact: "HIGH" }]} />)).toContain("NVIDIA beats estimates");
  });
  it("Fundamentals card: quality + NO DATA", () => {
    expect(r(<FundamentalsCard data={null} />)).toContain("NO DATA");
    expect(r(<FundamentalsCard data={{ symbol: "NVDA", company: null, quality_score: 78, quality_breakdown: null, financials: null, valuation: { market_cap: null, pe_ratio: 30, forward_pe: null, price_sales: null, enterprise_value: null }, analyst_estimates: { rating: "Buy", target_price: null, analyst_count: null, upgrade_count: null, downgrade_count: null }, strengths: ["Strong revenue growth"], risks: [] }} />)).toContain("78/100");
  });
  it("Options card: sentiment + NO DATA", () => {
    expect(r(<OptionsCard data={null} />)).toContain("NO DATA");
    expect(r(<OptionsCard data={{ symbol: "NVDA", options_score: 82, call_put_ratio: 1.4, implied_volatility: 0.45, volume: null, call_volume: null, put_volume: null, open_interest: null, premium_volume: null, unusual_activity: "Detected", unusual_activity_score: null, large_trade_count: null, sentiment: "Bullish", signals: ["Heavy call buying"], risks: [] }} />)).toContain("Bullish");
  });
  it("Traders card: consensus + NO DATA", () => {
    expect(r(<TradersCard data={null} />)).toContain("NO DATA");
    expect(r(<TradersCard data={{ symbol: "NVDA", consensus: "BULLISH", long_percent: 70, short_percent: 20, neutral_percent: 10, weighted_score: 74, contributor_count: 12, contributors: [{ id: "a", name: "x", quality: 80, strategy: null, market_focus: null, direction: "LONG" }] }} />)).toContain("BULLISH");
  });
  const inst: any = { symbol: "NVDA", status: "COMPLETE", institutional_changes: [{ institution: "Vanguard", symbol: "NVDA", previous_shares: 1000, current_shares: 1200, share_change: 200, percentage_change: 20, direction: "ACCUMULATION", filing_period: "Q2" }], institutional_direction: "ACCUMULATION", accumulation_score: 68, net_share_change_pct: 5, insider_activity: [{ insider_name: "CEO", title: "CEO", transaction_type: "BUY", shares: 100, price: 200, transaction_date: "2026-08-01" }], insider_sentiment: "BULLISH", insider_score: 60, insider_summary: { buy_count: 3, sell_count: 1, buy_shares: 1000, sell_shares: 200, distinct_buyers: 2 }, insider_cluster: { cluster_type: "ACCUMULATION", score: 65, insider_count: 3, summary: "3 insiders bought recently" } };
  it("Institutional + Insider Cluster cards: direction/cluster + NO DATA", () => {
    expect(r(<InstitutionalCard data={null} />)).toContain("NO DATA");
    expect(r(<InstitutionalCard data={inst} />)).toContain("ACCUMULATION");
    expect(r(<InsiderClusterCard data={null} />)).toContain("NO DATA");
    expect(r(<InsiderClusterCard data={inst} />)).toContain("3 insiders bought recently");
  });
});
