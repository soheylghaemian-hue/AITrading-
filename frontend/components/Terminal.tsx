"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSnapshot } from "../lib/useSnapshot";
import { emergencyStop, resumeTrading } from "../lib/api";
import {
  KpiGrid, MarketData, Funnel, Positions, AiTeam, AiAnalysis, RiskCenter, Performance,
  Governance, SystemHealth, Notifications, Reconciliation, MarketRegimes, Subscriptions,
} from "./sections";
import type { Snapshot } from "../lib/types";

const NAV = [
  ["/dashboard", "Overview"],
  ["/dashboard/positions", "Positions"],
  ["/dashboard/opportunities", "Opportunities"],
  ["/dashboard/agents", "AI Team"],
  ["/dashboard/risk", "Risk"],
  ["/dashboard/performance", "Performance"],
  ["/dashboard/governance", "Governance"],
  ["/dashboard/system", "System"],
  ["/dashboard/reconciliation", "Reconciliation"],
];

function StatusChip({ k, value, cls }: { k: string; value: string; cls: string }) {
  return <span className={`chip ${cls}`}><span className="dot" /><span className="k">{k}</span>{value}</span>;
}

function marketDataLabel(s: Snapshot | null): [string, string] {
  const f = s?.system_health?.market_data;
  if (f === "online") return ["REALTIME", "s-realtime"];
  if (f === "degraded") return ["PARTIAL", "s-partial"];
  if (!s) return ["NO DATA", "s-unavailable"];
  return ["UNAVAILABLE", "s-unavailable"];
}
function riskLabel(s: Snapshot | null): [string, string] {
  const r: any = s?.risk ?? {};
  if (r.killed) return ["KILLED", "s-killed"];
  if (r.halted) return ["HALTED", "s-halted"];
  if (!s) return ["NO DATA", "s-unavailable"];
  return ["ARMED", "s-armed"];
}
function systemLabel(s: Snapshot | null): [string, string] {
  const v = s?.system_status;
  if (v === "online") return ["ONLINE", "s-online"];
  if (v === "degraded") return ["DEGRADED", "s-degraded"];
  if (v === "halted") return ["HALTED", "s-halted"];
  return ["OFFLINE", "s-unavailable"];
}

export function Terminal({ view = "overview" }: { view?: string }) {
  const { data: s, connected } = useSnapshot();
  const [busy, setBusy] = useState(false);

  const [sysTxt, sysCls] = systemLabel(s);
  const [mdTxt, mdCls] = marketDataLabel(s);
  const [rkTxt, rkCls] = riskLabel(s);
  const mode = (s?.mode ?? "paper").toUpperCase();

  async function onEmergencyStop() {
    if (!confirm("EMERGENCY STOP: request the backend to trip the Risk Engine kill switch (blocks ALL new orders). Continue?")) return;
    const token = prompt("Owner token (ATP_DASHBOARD_TOKEN) — sent only to the backend:") || "";
    if (!token) return;
    setBusy(true);
    const res = await emergencyStop(token);
    setBusy(false);
    alert(res.ok ? `TRADING HALTED — ${res.detail}` : `Failed: ${res.detail}`);
  }

  const showAll = view === "overview";
  return (
    <>
      <header className="hdr">
        <span className="brand">AI TRADING <span className="dot2">COMMAND CENTER</span></span>
        <StatusChip k="MODE" value={mode} cls={mode === "LIVE" ? "s-halted" : "s-partial"} />
        <StatusChip k="SYSTEM" value={sysTxt} cls={sysCls} />
        <StatusChip k="BROKER" value="IBKR" cls={s?.risk && (s.risk as any).broker_connected ? "s-connected" : "s-unavailable"} />
        <StatusChip k="DATA" value={mdTxt} cls={mdCls} />
        <StatusChip k="RISK" value={rkTxt} cls={rkCls} />
        <span className="spacer" />
        <button className="estop" onClick={onEmergencyStop} disabled={busy}>■ Emergency Stop</button>
      </header>

      <nav className="nav">
        {NAV.map(([href, label]) => <NavLink key={href} href={href} label={label} />)}
      </nav>

      {!connected && (
        <div className="banner">
          ● Live backend not reachable — showing <strong>NO DATA</strong>. Configure
          <code style={{ margin: "0 4px" }}>NEXT_PUBLIC_API_BASE_URL</code> to a read-only backend. No values are fabricated.
        </div>
      )}

      <main>
        {(showAll) && <KpiGrid s={s} />}
        {(showAll || view === "market") && <MarketData s={s} />}
        {(showAll || view === "opportunities") && <Funnel s={s} />}
        {(showAll || view === "opportunities") && view !== "overview" && <MarketRegimes s={s} />}

        {showAll && (
          <div className="grid3">
            <Positions s={s} />
            <RiskCenter s={s} />
          </div>
        )}
        {view === "positions" && <Positions s={s} />}
        {view === "risk" && <RiskCenter s={s} />}

        {(showAll || view === "agents") && <AiTeam s={s} />}
        {(showAll || view === "agents") && <AiAnalysis s={s} />}

        {view === "performance" && <Performance s={s} />}
        {view === "governance" && <Governance s={s} />}
        {view === "reconciliation" && <Reconciliation s={s} />}
        {view === "system" && <SystemHealth s={s} />}

        {showAll && (
          <>
            <div className="grid2"><Performance s={s} /><Governance s={s} /></div>
            <Subscriptions s={s} />
            <div className="grid2"><SystemHealth s={s} /><Notifications s={s} /></div>
            <Reconciliation s={s} />
          </>
        )}
        {view === "market" && <Subscriptions s={s} />}
      </main>

      <footer>
        Read-only Command Center · the Risk Engine and Portfolio Manager are authoritative · no fabricated data<br />
        This public frontend never connects to IB Gateway, never places or cancels orders, and holds no credentials.
      </footer>
    </>
  );
}

function NavLink({ href, label }: { href: string; label: string }) {
  const path = usePathname();
  const active = path === href || (href === "/dashboard" && path === "/");
  return <Link href={href} className={active ? "active" : ""}>{label}</Link>;
}
