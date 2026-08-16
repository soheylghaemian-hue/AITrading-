// Macro Intelligence types (§ Phase R1.2). Mirrors the Control API's /macro/current and
// /market/{symbol}/macro-context. Read-only intelligence input — the global macro environment + risk
// regime (RISK_ON / RISK_NEUTRAL / RISK_OFF). Missing metrics → NO DATA, never fabricated.

export type MacroRegime = "RISK_ON" | "RISK_NEUTRAL" | "RISK_OFF";

export interface MacroMetric {
  label: string;
  value: number | null;
  trend: "up" | "down" | "flat" | null;
}

export interface Macro {
  score: number | null;
  regime: MacroRegime | null;
  status: string | null;                 // COMPLETE / PARTIAL / NO DATA
  signals: string[];
  risks: string[];
  metrics: Record<string, MacroMetric>;
  timestamp: string | null;
  source: string | null;
}

export interface MacroContext {
  symbol: string;
  regime: MacroRegime | null;
  score: number | null;
  relevance: string | null;              // TAILWIND / NEUTRAL / HEADWIND
  note: string | null;
  status: string | null;
  signals: string[];
  risks: string[];
}

export function hasMacro(m: Macro | MacroContext | null | undefined): boolean {
  return !!m && m.regime != null;
}

/** Tone class suffix for a regime — drives badge/border colour. */
export function regimeTone(r: string | null | undefined): "on" | "neutral" | "off" | "neu" {
  return r === "RISK_ON" ? "on" : r === "RISK_NEUTRAL" ? "neutral" : r === "RISK_OFF" ? "off" : "neu";
}

/** Human label, e.g. "RISK ON". */
export function regimeLabel(r: string | null | undefined): string {
  return r ? r.replace(/_/g, " ") : "NO DATA";
}

export function relevanceTone(rel: string | null | undefined): "on" | "neutral" | "off" | "neu" {
  return rel === "TAILWIND" ? "on" : rel === "NEUTRAL" ? "neutral" : rel === "HEADWIND" ? "off" : "neu";
}
