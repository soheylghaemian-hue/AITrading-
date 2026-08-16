"use client";
// App shell: owns the single useSnapshot() poll, provides it via context, and renders the sidebar +
// status strip around every page. Client-only (hooks). Presentational views stay pure & testable.
import React, { createContext, useContext, useState } from "react";
import { usePathname } from "next/navigation";
import { useSnapshot } from "@/lib/useSnapshot";
import { emergencyStop } from "@/lib/api";
import type { SnapshotState } from "@/lib/types";
import { statusStrip } from "@/lib/select";
import { StatusPill } from "./ui";

const Ctx = createContext<SnapshotState>({ data: null, loading: true, connected: false, error: null, lastFetch: null });
export function useDashboard() { return useContext(Ctx); }

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const state = useSnapshot();
  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

const NAV = [
  { href: "/", label: "Overview", d: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z" },
  { href: "/markets", label: "Markets", d: "M3 17l5-6 4 4 5-8 4 5" },
  { href: "/portfolio", label: "Portfolio", d: "M3 7h18v13H3zM8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" },
  { href: "/ai-brain", label: "AI Brain", d: "M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 0V6a2 2 0 0 0-3-2ZM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 0" },
  { href: "/risk", label: "Risk Center", d: "M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7ZM12 8v4M12 15v.5" },
  { href: "/system", label: "System", d: "M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" },
];

function titleFor(path: string): [string, string] {
  if (path === "/") return ["Overview", "Real-time state of your AI trading system"];
  if (path.startsWith("/markets/")) return [`${decodeURIComponent(path.split("/")[2] || "")} · Market Intelligence`, "Realtime quote, data quality and AI context"];
  if (path.startsWith("/markets")) return ["Markets", "Live market intelligence and data quality"];
  if (path.startsWith("/portfolio")) return ["Portfolio", "Capital, exposure and open positions"];
  if (path.startsWith("/ai-brain")) return ["AI Brain", "Human-readable decisions and agent reasoning"];
  if (path.startsWith("/risk")) return ["Risk Center", "Your risk mandate and live budget"];
  if (path.startsWith("/system")) return ["System", "Service health and diagnostics"];
  return ["Overview", ""];
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname() || "/";
  const { data, connected } = useDashboard();
  const [stopping, setStopping] = useState(false);
  const [title, sub] = titleFor(path);
  const pills = statusStrip(data, connected);
  const mode = pills.find((p) => p.key === "mode")!;
  const system = pills.find((p) => p.key === "system")!;

  // The branded /login page owns the full screen — render it without the dashboard chrome. (All
  // hooks above run unconditionally, so this early return never changes hook order.)
  if (path === "/login") return <>{children}</>;

  async function onSignOut() {
    await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.assign("/login");
  }

  // Emergency control (not a normal button): a guarded, confirmed action. The kill switch is
  // authoritative in the BACKEND RiskEngine — the browser only sends a token-authenticated request.
  async function onStop() {
    const ok = window.confirm(
      "EMERGENCY STOP\n\nThis engages the backend kill switch and halts ALL trading immediately.\n\nAre you sure you want to proceed?");
    if (!ok) return;
    setStopping(true);
    try { const r = await emergencyStop(); window.alert(r.ok ? "Kill switch engaged — trading halted." : `Not engaged: ${r.detail}`); }
    finally { setStopping(false); }
  }

  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          <span className="logo"><svg viewBox="0 0 34 34" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#35D0BA" /><stop offset="1" stopColor="#0E8C7E" /></linearGradient></defs><path d="M17 2 30 9.5v15L17 32 4 24.5v-15Z" fill="none" stroke="url(#lg)" strokeWidth="1.6" /><path d="M17 9 24 13v8l-7 4-7-4v-8Z" fill="none" stroke="#35D0BA" strokeWidth="1.3" opacity=".7" /><circle cx="17" cy="17" r="2.4" fill="#35D0BA" /></svg></span>
          <div><h1>GIGBAY&nbsp;AI</h1><p>AI Trading System</p></div>
        </div>
        <nav>
          {NAV.map((it) => (
            <a className={`nav-i${active(it.href) ? " on" : ""}`} href={it.href} key={it.href} aria-current={active(it.href) ? "page" : undefined}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d={it.d} /></svg>{it.label}
            </a>
          ))}
        </nav>
        <div className="side-foot">
          <div className="modecard"><div className="label">Trading Mode</div><div className="mode-paper"><span className={`dot ${mode.tone}`} />{mode.value === "NO DATA" ? "NO DATA" : mode.value + " TRADING"}</div></div>
          <div className="modecard"><div className="label">System Status</div><div className="sys-run"><span className={`dot ${system.tone}`} /><b style={{ color: "var(--ink)" }}>{system.value}</b></div></div>
          <button className="stop" onClick={onStop} disabled={stopping || !connected} aria-label="Emergency stop — engage backend kill switch, halts all trading" title="Emergency control — requires confirmation">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2" /></svg>
            {stopping ? "…" : "EMERGENCY STOP"}
          </button>
          <button className="signout" onClick={onSignOut} aria-label="Sign out">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M15 17l5-5-5-5M20 12H9M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3" /></svg>
            Sign out
          </button>
        </div>
      </aside>
      <div className="main">
        <header className="top">
          <div><h2>{title}</h2><div className="sub">{sub}</div></div>
          <div className="strip">{pills.map((p) => <StatusPill key={p.key} label={p.label} value={p.value} tone={p.tone} />)}</div>
        </header>
        <div className="wrap">{children}</div>
      </div>
    </div>
  );
}
