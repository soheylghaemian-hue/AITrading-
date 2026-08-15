// Pure selectors: derive per-view models from a Snapshot (or null). NO DATA discipline lives here —
// nothing is fabricated; absent values stay absent so the views render NO DATA. Unit-tested.
import type { Snapshot } from "./types";

export type Tone = "g" | "t" | "o" | "r" | "grey";
export interface Pill { key: string; label: string; value: string; tone: Tone; }

/** The top status strip. Always includes an explicit EXECUTION pill. Off/unreachable → honest NO DATA. */
export function statusStrip(s: Snapshot | null, connected: boolean): Pill[] {
  if (!connected || !s) {
    return [
      { key: "system", label: "SYSTEM", value: "OFFLINE", tone: "grey" },
      { key: "mode", label: "MODE", value: "NO DATA", tone: "grey" },
      { key: "execution", label: "EXECUTION", value: "DISABLED", tone: "grey" },
      { key: "data", label: "DATA", value: "NO DATA", tone: "grey" },
      { key: "broker", label: "BROKER", value: "NO DATA", tone: "grey" },
    ];
  }
  const isLive = (s.mode || "").toUpperCase() === "LIVE";
  const execEnabled = s.execution_enabled === true;
  const broker = s.connected === true;
  const realtime =
    (s.global_market_data || []).some((r) => r.realtime && r.status !== "DATA_NOT_AVAILABLE") ||
    (s.market_data || []).some((r) => r.market_data_type === "REALTIME" && r.status === "DATA_AVAILABLE");
  const sysRaw = (s.system_status || "").toString();
  const system = /online|ready|running/i.test(sysRaw) ? (/running/i.test(sysRaw) ? "RUNNING" : "READY")
    : sysRaw ? sysRaw.toUpperCase() : "READY";
  return [
    { key: "system", label: "SYSTEM", value: system, tone: "g" },
    { key: "mode", label: "MODE", value: isLive ? "LIVE" : "PAPER", tone: isLive ? "r" : "t" },
    { key: "execution", label: "EXECUTION", value: execEnabled ? "ENABLED" : "DISABLED", tone: execEnabled ? "o" : "grey" },
    { key: "data", label: "DATA", value: realtime ? "REALTIME" : "NO DATA", tone: realtime ? "t" : "grey" },
    { key: "broker", label: "BROKER", value: broker ? "CONNECTED" : "DISCONNECTED", tone: broker ? "g" : "r" },
  ];
}

export type RiskHealth = "HEALTHY" | "WARNING" | "BLOCKED" | "NO DATA";

/** Overall risk-health indicator for the Risk Center. */
export function riskHealth(s: Snapshot | null): RiskHealth {
  const tr = s?.trading_risk;
  if (!tr) return "NO DATA";
  if (tr.status === "DAILY LOSS LIMIT REACHED") return "BLOCKED";
  const used = tr.max_daily_loss > 0 ? Math.max(0, -tr.current_daily_pnl) / tr.max_daily_loss : 0;
  return used >= 0.75 ? "WARNING" : "HEALTHY";
}

/** Fraction (0..1) of the daily-loss budget used, or null if unknown. */
export function dailyRiskUsed(s: Snapshot | null): number | null {
  const tr = s?.trading_risk;
  if (!tr || !(tr.max_daily_loss > 0)) return null;
  return Math.min(1, Math.max(0, -tr.current_daily_pnl) / tr.max_daily_loss);
}

/** The AI engine lifecycle state, honestly (never invents RUNNING). */
export function engineState(s: Snapshot | null): "DISABLED" | "ARMED" | "RUNNING" | "HALTED" | "KILLED" | "NO DATA" {
  const a = s?.autonomous;
  if (!a || !a.status) return "NO DATA";
  return a.status;
}

export function isLiveMode(s: Snapshot | null): boolean {
  return (s?.mode || "").toUpperCase() === "LIVE";
}
