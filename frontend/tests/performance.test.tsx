import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { PerformanceView } from "@/components/AiPerformance";
import { AiHistoryList } from "@/components/terminal/AiHistoryFeed";
import { fetchPerformance, fetchAiHistory, fetchOutcomes } from "@/lib/api";
import { accTone, gatedAccTone, gatedVerdict, hasHistory, hasPerformance, type AiHistory,
  type AiPerformance } from "@/lib/performance";
import { NOT_VALIDATED, type ValidationGate } from "@/lib/validation";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

// § R3.1A.2 — the ONLY gate that unlocks a positive verdict on the legacy panel.
const VALIDATED: ValidationGate = { validated: true, reason: "VALIDATED", run_id: "RV1",
  status: "COMPLETED", created_at: "2026-08-20T06:40:00Z" };
const GATE_FAILED: ValidationGate = { validated: false, reason: "GATE_NOT_PASSED", run_id: "RV1",
  status: "COMPLETED", created_at: "2026-08-20T06:40:00Z" };

const PERF: AiPerformance = {
  sample_size: 100, overall_accuracy: 67, direction_accuracy: 67, bullish_accuracy: 70, bearish_accuracy: 60,
  average_return: 1.8,
  confidence_calibration: { high: { count: 40, success_rate: 74, avg_confidence: 90 },
    medium: { count: 35, success_rate: 62, avg_confidence: 70 }, low: { count: 25, success_rate: 51, avg_confidence: 50 },
    verdict: "Overconfident" },
  score_reliability: { high_score_accuracy: 72, low_score_accuracy: 55 }, horizon_days: 5,
  errors: { "FALSE BULLISH": 8, "CONFLICT FAILURE": 3 }, best_inputs: ["Fundamentals", "Options"], weakest_inputs: ["News"],
  by_horizon: {
    "1": { accuracy: 58, average_return: 0.6, sample_size: 100 },
    "3": { accuracy: 63, average_return: 1.1, sample_size: 98 },
    "5": { accuracy: 67, average_return: 1.8, sample_size: 95 },
    "20": { accuracy: 71, average_return: 4.2, sample_size: 80 },
  },
};
const EMPTY_PERF: AiPerformance = {
  sample_size: 0, overall_accuracy: null, direction_accuracy: null, bullish_accuracy: null, bearish_accuracy: null,
  average_return: null, confidence_calibration: null, score_reliability: null, horizon_days: 5,
  errors: {}, best_inputs: [], weakest_inputs: [],
};
const HIST: AiHistory = {
  symbol: "NVDA", count: 2, assessments: [
    { id: "NVDA:2026-08-16T10", symbol: "NVDA", timestamp: "2026-08-16T10:00:00Z", score: 83, direction: "BULLISH",
      confidence: 83, status: "COMPLETE", price_at_prediction: 200,
      outcomes: [{ time_horizon: 5, future_price: 206.2, return_percentage: 3.1, direction_correct: true }] },
    { id: "NVDA:2026-08-10T10", symbol: "NVDA", timestamp: "2026-08-10T10:00:00Z", score: 60, direction: "BEARISH",
      confidence: 60, status: "PARTIAL", price_at_prediction: 210, outcomes: [] },
  ],
};
const EMPTY_HIST: AiHistory = { symbol: "NVDA", count: 0, assessments: [] };

describe("performance helpers", () => {
  it("detect data + accuracy tone", () => {
    expect(hasPerformance(EMPTY_PERF)).toBe(false);
    expect(hasPerformance(PERF)).toBe(true);
    expect(hasHistory(EMPTY_HIST)).toBe(false);
    expect(hasHistory(HIST)).toBe(true);
    expect(accTone(67)).toBe("pos");
    expect(accTone(40)).toBe("neg");
    expect(accTone(null)).toBe("neu");
  });
});

describe("PerformanceView — honest evaluation, never fabricated", () => {
  it("renders accuracy, average return, calibration and best/weakest inputs", () => {
    const h = r(<PerformanceView data={PERF} loading={false} error={null} gate={VALIDATED} />);
    expect(h).toContain("AI Performance");
    expect(h).toContain("Last 100");
    expect(h).toContain("67%");                      // directional accuracy
    expect(h).toContain("+1.8%");                    // average return
    expect(h).toContain("Overconfident");            // calibration verdict
    expect(h).toContain("Fundamentals");             // best input
    expect(h).toContain("FALSE BULLISH: 8");         // error breakdown
    // §G3.2 per-horizon accuracy (1/3/5/20-day)
    expect(h).toContain("1-Day Accuracy");
    expect(h).toContain("20-Day Accuracy");
    expect(h).toContain("58%");                       // 1-day accuracy
    expect(h).toContain("71%");                       // 20-day accuracy
  });
  it("shows NO DATA with too few evaluated predictions (no fabricated metrics)", () => {
    const h = r(<PerformanceView data={EMPTY_PERF} loading={false} error={null} gate={VALIDATED} />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("Not enough evaluated");
    expect(h).toContain("INSUFFICIENT DATA");
    expect(h).not.toContain("Overconfident");
  });
  it("shows LOADING while evaluating", () => {
    expect(r(<PerformanceView data={null} loading error={null} />)).toContain("LOADING");
  });
});

// § R3.1A.2 — the legacy panel is labelled LEGACY and carries NO positive verdict without a passed gate.
describe("PerformanceView — LEGACY labelling + validation gating", () => {
  const render = (gate?: ValidationGate | null) =>
    r(<PerformanceView data={PERF} loading={false} error={null} gate={gate as any} />);

  it("labels the panel and every metric LEGACY", () => {
    const h = render(VALIDATED);
    expect(h).toContain("LEGACY METRICS");
    // 4 headline stats + 4 per-horizon stats + panel header + calibration header.
    expect((h.match(/LEGACY/g) || []).length).toBeGreaterThanOrEqual(10);
    expect(h).toContain("Directional Accuracy");
    expect(h).toContain('<span class="vd-ins-tag">LEGACY</span>');
  });

  it("suppresses the calibration verdict and shows NOT VALIDATED / INSUFFICIENT DATA with no run", () => {
    const h = render({ ...NOT_VALIDATED, reason: "NO_COMPLETED_RUN" });
    expect(h).not.toContain("Overconfident");
    expect(h).toContain("NOT VALIDATED");
    expect(h).toContain("INSUFFICIENT DATA");
    expect(h).toContain("No COMPLETED validation run exists yet.");
  });

  it("stays NOT VALIDATED when the latest COMPLETED run did not pass its gate", () => {
    const h = render(GATE_FAILED);
    expect(h).toContain("NOT VALIDATED");
    expect(h).not.toContain("Overconfident");
    expect(h).toContain("did NOT pass its preregistered gate");
    expect(h).not.toContain(">VALIDATED<");
  });

  it("fails closed when the gate prop is missing or null (unknown ⇒ NOT VALIDATED)", () => {
    for (const h of [r(<PerformanceView data={PERF} loading={false} error={null} />), render(null)]) {
      expect(h).toContain("NOT VALIDATED");
      expect(h).not.toContain("Overconfident");
    }
  });

  it("renders the verdict only once the gate passes, and withholds positive tones until then", () => {
    const passed = render(VALIDATED);
    expect(passed).toContain("Overconfident");
    expect(passed).toContain("num pos");                   // 67% directional accuracy reads positive
    expect(passed).toContain("num up");                    // +1.8% average return reads positive
    const blocked = render(GATE_FAILED);
    expect(blocked).not.toContain("num pos");              // no green verdict without validation
    expect(blocked).not.toContain("num up");
    expect(blocked).toContain("67%");                      // the number itself is still reported honestly
    expect(blocked).toContain("+1.8%");
  });

  it("gated helpers: verdict + tone are pure and fail closed", () => {
    expect(gatedVerdict(PERF.confidence_calibration, true)).toBe("Overconfident");
    expect(gatedVerdict(PERF.confidence_calibration, false)).toBe("NOT VALIDATED");
    expect(gatedVerdict(null, true)).toBe("NO DATA");
    expect(gatedAccTone(90, true)).toBe("pos");
    expect(gatedAccTone(90, false)).toBe("neu");           // never positive while unvalidated
    expect(gatedAccTone(20, false)).toBe("neg");           // a negative reading stays honest
    expect(accTone(90)).toBe("pos");
  });
});

describe("AiHistoryList — immutable past views with outcomes", () => {
  it("renders past predictions with their measured outcomes", () => {
    const h = r(<AiHistoryList data={HIST} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("Aug");                      // date
    expect(h).toContain("BULLISH");
    expect(h).toContain("Score 83");
    expect(h).toContain("3.1% after 5d");            // outcome
    expect(h).toContain("BEARISH");
    expect(h).toContain("pending");                  // unmatured prediction
  });
  it("shows NO DATA with no history (nothing fabricated)", () => {
    const h = r(<AiHistoryList data={EMPTY_HIST} loading={false} error={null} symbol="NVDA" />);
    expect(h).toContain("NO DATA");
    expect(h).toContain("No past AI views");
  });
});

describe("fetch endpoints — via the same-origin proxy only", () => {
  let calls: string[] = [];
  const okFetch = (body: any) =>
    vi.fn(async (url: string) => { calls.push(String(url)); return { ok: true, status: 200, json: async () => body } as any; });
  beforeEach(() => { calls = []; });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("fetchPerformance hits /api/dashboard/ai-performance", async () => {
    vi.stubGlobal("fetch", okFetch(PERF));
    const res = await fetchPerformance(5);
    expect(calls[0]).toBe("/api/dashboard/ai-performance?horizon=5");
    expect(res.direction_accuracy).toBe(67);
    // §G3.2: the per-horizon grid must survive the field-by-field rebuild (regression).
    expect(res.by_horizon?.["20"].accuracy).toBe(71);
  });
  it("fetchAiHistory hits /api/dashboard/ai-history/{symbol}", async () => {
    vi.stubGlobal("fetch", okFetch(HIST));
    const res = await fetchAiHistory("NVDA");
    expect(calls[0]).toBe("/api/dashboard/ai-history/NVDA");
    expect(res.assessments.length).toBe(2);
  });
  it("fetchOutcomes hits /api/dashboard/ai-outcomes (OLC status)", async () => {
    vi.stubGlobal("fetch", okFetch({ prediction_count: 12, evaluated_count: 40, pending_count: 8, accuracy: 75,
      horizons: [1, 3, 5, 20], classification: { "TRUE POSITIVE": 6, "FALSE POSITIVE": 2 } }));
    const res = await fetchOutcomes();
    expect(calls[0]).toBe("/api/dashboard/ai-outcomes");
    expect(res.pending_count).toBe(8);
    expect(res.accuracy).toBe(75);
  });
  it("rejects on a non-OK response (caller shows NO DATA)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) } as any)));
    await expect(fetchPerformance(5)).rejects.toBeTruthy();
  });
});
