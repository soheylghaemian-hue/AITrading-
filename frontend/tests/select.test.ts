import { describe, it, expect } from "vitest";
import type { Snapshot } from "@/lib/types";
import { statusStrip, riskHealth, dailyRiskUsed, engineState } from "@/lib/select";

const base: Snapshot = {
  mode: "paper", system_status: "online", connected: true, execution_enabled: false,
  trading_risk: {
    capital: 1_000_000, risk_per_trade_pct: 0.01, max_risk_per_trade: 10_000,
    max_daily_loss_pct: 0.03, max_daily_loss: 30_000, current_daily_pnl: -8_200,
    remaining_daily_risk: 21_800, status: "ACTIVE",
  },
  autonomous: { status: "ARMED" } as any,
};

describe("status strip — explicit execution state + paper/live distinction", () => {
  it("paper mode with execution disabled reads PAPER + DISABLED", () => {
    const pills = statusStrip(base, true);
    const by = (k: string) => pills.find((p) => p.key === k)!;
    expect(by("mode").value).toBe("PAPER");
    expect(by("mode").tone).toBe("t");
    expect(by("execution").value).toBe("DISABLED");
    expect(by("system").value).toBe("READY");
    expect(by("broker").value).toBe("CONNECTED");
  });

  it("LIVE mode with execution enabled reads LIVE + ENABLED (distinct, warning tone)", () => {
    const pills = statusStrip({ ...base, mode: "LIVE", execution_enabled: true }, true);
    const by = (k: string) => pills.find((p) => p.key === k)!;
    expect(by("mode").value).toBe("LIVE");
    expect(by("mode").tone).toBe("r");
    expect(by("execution").value).toBe("ENABLED");
    expect(by("execution").tone).toBe("o");
  });

  it("disconnected shows honest NO DATA / OFFLINE, execution still explicitly DISABLED", () => {
    const pills = statusStrip(null, false);
    const by = (k: string) => pills.find((p) => p.key === k)!;
    expect(by("system").value).toBe("OFFLINE");
    expect(by("mode").value).toBe("NO DATA");
    expect(by("execution").value).toBe("DISABLED");
    expect(pills.every((p) => p.tone === "grey")).toBe(true);
  });
});

describe("risk health + budget", () => {
  it("classifies HEALTHY / WARNING / BLOCKED and NO DATA", () => {
    expect(riskHealth(base)).toBe("HEALTHY");
    expect(riskHealth({ ...base, trading_risk: { ...base.trading_risk!, current_daily_pnl: -24_000 } })).toBe("WARNING");
    expect(riskHealth({ ...base, trading_risk: { ...base.trading_risk!, status: "DAILY LOSS LIMIT REACHED" } })).toBe("BLOCKED");
    expect(riskHealth(null)).toBe("NO DATA");
  });
  it("daily risk used is a bounded fraction or null", () => {
    expect(dailyRiskUsed(base)).toBeCloseTo(0.273, 2);
    expect(dailyRiskUsed(null)).toBeNull();
  });
  it("engine state is honest (NO DATA, never invented RUNNING)", () => {
    expect(engineState(base)).toBe("ARMED");
    expect(engineState(null)).toBe("NO DATA");
  });
});
