"use client";
// Risk Center page (§ Phase R2.0) — the capital-protection Risk Control Center. Read-only w.r.t.
// trading: it observes limits, live budget usage, the kill switch and an immutable audit trail, and
// lets you edit the limits. It never places an order, never enables execution, never touches the
// broker / IBKR, and never mutates the kill switch. `connected` comes from the shared snapshot poll.
import { useDashboard } from "@/components/shell";
import { RiskControlCenter } from "@/components/RiskControlCenter";

export default function Page() {
  const { connected } = useDashboard();
  return <RiskControlCenter connected={connected} />;
}
