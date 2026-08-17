// Risk Control Center types + helpers (§ Phase R2.0). Mirrors the Control API's read-only
// /risk/status, /risk/config and /risk/events. This is a capital-protection OBSERVABILITY +
// governance-gate layer: it shows limits, live budget usage and an immutable config/kill-switch
// audit trail. It never trades, never places or submits an order, never enables execution, and
// never mutates the kill switch. Missing inputs render as NO DATA — never zero, never READY.
//
// Units (important): config percentages are WHOLE numbers (2.0 = 2%). daily_pnl.used_pct,
// exposure.*_pct and drawdown.*_pct are also whole-number percents (0..100+). Money is absolute.

export type RiskState = "READY" | "WARNING" | "BLOCKED" | "NO DATA";

export interface RiskStatus {
  status: RiskState | string | null;
  reasons: string[];
  missing: string[];
  capital: { value: number | null; currency: string | null; source: string | null };
  daily_pnl: { value: number | null; limit: number | null; used_pct: number | null; remaining: number | null; observed_at: string | null };
  position_risk: { value: number | null; limit: number | null };
  exposure: { gross_pct: number | null; net_pct: number | null; limit_pct: number | null };
  drawdown: { value_pct: number | null; limit_pct: number | null };
  kill_switch: "STOPPED" | "ARMED" | string | null;
  configuration_version: number | null;
  version_token: string | null;
  updated_at: string | null;
  ts: string | null;
}

export interface RiskConfig {
  capital: number | null;
  currency: string | null;
  max_daily_loss_pct: number | null;
  max_daily_loss_amount: number | null;        // DERIVED on the backend (never persisted)
  max_daily_loss_amount_basis: string | null;
  max_position_risk_pct: number | null;
  max_portfolio_exposure_pct: number | null;
  max_drawdown_pct: number | null;
  warning_threshold_pct: number | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface RiskConfigView {
  configured: boolean;
  reason: string | null;
  configuration_version: number | null;
  version_token: string | null;
  kill_switch: "STOPPED" | "ARMED" | string | null;
  config: RiskConfig | null;
}

export interface RiskEvent {
  id: number | string | null;
  timestamp: string | null;
  event_type: string | null;
  severity: string | null;
  description: string | null;
  reason_code: string | null;
  observed_value: number | null;
  configured_limit: number | null;
  configuration_version: number | null;
  details_json: string | null;
  source: string | null;
}

export interface RiskEvents {
  count: number;
  events: RiskEvent[];
}

/** True when the backend actually returned a risk state (never fabricate one). */
export function hasRiskState(s: RiskStatus | null | undefined): boolean {
  return !!s && s.status != null;
}

/** State → CSS tone suffix used by the cards/badges. */
export function stateTone(status: string | null | undefined): "ready" | "warning" | "blocked" | "nodata" {
  return status === "READY" ? "ready"
    : status === "WARNING" ? "warning"
    : status === "BLOCKED" ? "blocked"
    : "nodata";
}

/** Whole-number percent formatter (2 → "2%", 80.4 → "80.4%"). NOT the fraction ×100 of format.pct. */
export function pctNum(v: unknown, digits = 1): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "NO DATA";
  const s = v.toFixed(digits);
  return (s.endsWith("." + "0".repeat(digits)) ? String(Math.round(v)) : s) + "%";
}

/** Currency-aware money, or NO DATA (never a fabricated 0). Falls back to a plain number if the
 *  currency code is unknown to Intl. A real observed 0 is shown; a missing value is NO DATA. */
export function moneyIn(v: unknown, currency: string | null | undefined, digits = 0): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "NO DATA";
  const cur = (currency || "").toUpperCase();
  try {
    if (cur) return v.toLocaleString("en-US", { style: "currency", currency: cur, minimumFractionDigits: digits, maximumFractionDigits: digits });
  } catch { /* unknown currency → plain number below */ }
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) + (cur ? ` ${cur}` : "");
}

/** Daily-budget usage as a 0..1 fraction for the gauge, or null (→ NO DATA). used_pct is 0..100. */
export function usedFrac(used_pct: number | null | undefined): number | null {
  if (typeof used_pct !== "number" || !Number.isFinite(used_pct)) return null;
  return Math.min(1, Math.max(0, used_pct / 100));
}

// Machine reason codes → human, capital-protection wording. Unknown codes fall through verbatim.
const REASON_LABELS: Record<string, string> = {
  KILL_SWITCH_TRIGGERED: "Kill switch engaged — trading halted",
  RISK_CONFIGURATION_MISSING: "No risk configuration set",
  RISK_DATA_MISSING: "Live risk inputs unavailable",
  DAILY_LOSS_LIMIT_EXCEEDED: "Daily loss limit reached",
  EXPOSURE_LIMIT_EXCEEDED: "Portfolio exposure limit exceeded",
  DRAWDOWN_LIMIT_EXCEEDED: "Max drawdown limit exceeded",
  POSITION_RISK_EXCEEDED: "Single-position risk limit exceeded",
  DAILY_LOSS_WARNING: "Approaching daily loss limit",
  EXPOSURE_WARNING: "Approaching exposure limit",
  DRAWDOWN_WARNING: "Approaching drawdown limit",
};

export function reasonLabel(code: string | null | undefined): string {
  if (!code) return "";
  return REASON_LABELS[code] ?? code.replace(/_/g, " ").toLowerCase();
}

export function severityTone(sev: string | null | undefined): "blocked" | "warning" | "ready" | "nodata" {
  const s = (sev || "").toUpperCase();
  return s === "CRITICAL" ? "blocked" : s === "WARNING" ? "warning" : s === "INFO" ? "ready" : "nodata";
}

/** A short human title for an event type (config update, kill switch, …). */
export function eventTitle(t: string | null | undefined): string {
  switch (t) {
    case "CONFIGURATION_UPDATED": return "Configuration updated";
    case "KILL_SWITCH_TRIGGERED": return "Kill switch engaged";
    case "KILL_SWITCH_ARMED": return "Kill switch armed";
    default: return t ? t.replace(/_/g, " ") : "Event";
  }
}
