// Capital Readiness composer (§ Phase UX-1) — PURE, side-effect-free. Answers "is the system ready to
// deploy capital?" by composing ONLY real read-model fields: data completeness, AI governance, risk
// configuration, risk-data availability, broker/portfolio-data availability and execution state.
//
// Cardinal rule: READY is NEVER inferred from the mere absence of positions. It requires every input to
// be affirmatively present and passing. In production (risk configuration missing, no live risk data,
// execution disabled) this correctly yields NOT READY with honest reasons. Missing inputs → NO DATA,
// never zero, never a false READY. No trade/order/broker/execution call — read-only derivation.
import type { Snapshot } from "./types";
import type { Completeness } from "./completeness";
import type { Governance } from "./governance";
import type { RiskStatus, RiskConfigView } from "./risk";

export type CheckState = "ok" | "warn" | "bad" | "nodata";

export interface ReadinessCheck {
  key: string;
  label: string;
  state: CheckState;
  detail: string;
}

export interface Readiness {
  label: "READY" | "NOT READY" | "NO DATA";
  ready: boolean;
  checks: ReadinessCheck[];
  reasons: string[];
}

export interface ReadinessInputs {
  snapshot: Snapshot | null;
  connected: boolean;
  completeness?: Completeness | null;
  governance?: Governance | null;
  riskStatus?: RiskStatus | null;
  riskConfig?: RiskConfigView | null;
}

export function computeReadiness(inp: ReadinessInputs): Readiness {
  const { snapshot: s, connected } = inp;
  const checks: ReadinessCheck[] = [];

  // 1) Data Completeness
  const c = inp.completeness;
  checks.push({
    key: "completeness", label: "Data Completeness",
    state: c?.state == null ? "nodata" : c.state === "READY" ? "ok" : c.state === "PARTIAL" ? "warn" : "bad",
    detail: c?.score == null ? "NO DATA" : `${c.state} · ${c.score}/100`,
  });

  // 2) AI Governance
  const g = inp.governance;
  checks.push({
    key: "governance", label: "AI Governance",
    state: g?.status == null ? "nodata" : g.status === "APPROVED" ? "ok"
      : g.status === "BLOCKED" ? "bad" : "warn",
    detail: g?.status ?? "NO DATA",
  });

  // 3) Risk Configuration
  const rc = inp.riskConfig;
  checks.push({
    key: "risk_config", label: "Risk Configuration",
    state: rc == null ? "nodata" : rc.configured ? "ok" : "bad",
    detail: rc == null ? "NO DATA" : rc.configured ? `Configured · v${rc.configuration_version ?? "?"}` : "Not configured",
  });

  // 4) Risk Data Availability (are the live risk inputs present?)
  const rs = inp.riskStatus;
  const riskDataAvail = rs?.status != null && rs.status !== "NO DATA";
  checks.push({
    key: "risk_data", label: "Risk Data Availability",
    state: rs?.status == null ? "nodata" : riskDataAvail ? "ok" : "bad",
    detail: rs?.status == null ? "NO DATA"
      : riskDataAvail ? "Live risk inputs present"
      : (rs.missing.length ? `Missing: ${rs.missing.join(", ")}` : "Unavailable"),
  });

  // 5) Broker / Portfolio Data Availability — requires an ACTIVE broker connection AND real portfolio
  // data. A stale account id on a disconnected broker is not "available" (honest, not optimistic).
  const brokerConnected = s?.connected === true;
  const hasPortfolio = brokerConnected && !!(s && (s.account || (s.positions && s.positions.length > 0)));
  checks.push({
    key: "broker", label: "Broker / Portfolio Data",
    state: !connected ? "nodata" : hasPortfolio ? "ok" : brokerConnected ? "warn" : "bad",
    detail: !connected ? "NO DATA"
      : hasPortfolio ? "Account / positions available"
      : brokerConnected ? "Broker connected · no portfolio data" : "Broker disconnected",
  });

  // 6) Execution State — DISABLED is the safe, intended state; it still means NOT ready to deploy capital.
  const execEnabled = s?.execution_enabled === true;
  checks.push({
    key: "execution", label: "Execution State",
    state: !connected ? "nodata" : execEnabled ? "ok" : "warn",
    detail: !connected ? "NO DATA" : execEnabled ? "ENABLED" : "DISABLED (safe)",
  });

  const allNoData = checks.every((k) => k.state === "nodata");
  const ready = checks.every((k) => k.state === "ok");
  const reasons = checks.filter((k) => k.state !== "ok").map((k) => `${k.label.toLowerCase()}: ${k.detail}`);
  return {
    label: allNoData ? "NO DATA" : ready ? "READY" : "NOT READY",
    ready, checks, reasons,
  };
}

export function readinessTone(label: string): "ready" | "warning" | "blocked" | "nodata" {
  return label === "READY" ? "ready" : label === "NOT READY" ? "blocked" : "nodata";
}

export function checkTone(state: CheckState): "ready" | "warning" | "blocked" | "nodata" {
  return state === "ok" ? "ready" : state === "warn" ? "warning" : state === "bad" ? "blocked" : "nodata";
}
