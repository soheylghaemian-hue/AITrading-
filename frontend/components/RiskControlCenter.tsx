"use client";
// Risk Control Center (§ Phase R2.0) — the capital-protection OBSERVABILITY + governance-gate page.
// It shows the live risk state (READY / WARNING / BLOCKED / NO DATA), the daily-loss budget, every
// limit vs its observed usage, the authoritative kill switch, an immutable config/kill-switch audit
// trail, and the limit-editing form. It is strictly READ-ONLY with respect to trading: it never
// places or submits an order, never touches the broker / IBKR / execution / autonomous paths, and
// never mutates the kill switch. Missing inputs render as NO DATA — never zero, never a false READY.
import React, { useCallback, useEffect, useState } from "react";
import { NO_DATA } from "@/lib/format";
import { GaugeArc } from "@/components/ui";
import {
  fetchRiskStatus, fetchRiskConfig, fetchRiskEvents,
} from "@/lib/api";
import {
  type RiskStatus, type RiskConfigView, type RiskEvents,
  stateTone, pctNum, usedFrac, moneyIn, reasonLabel, severityTone, eventTitle, hasRiskState,
} from "@/lib/risk";
import { RiskConfigForm } from "./RiskConfigForm";

const STATE_TITLE: Record<string, string> = {
  ready: "PROTECTED", warning: "CAUTION", blocked: "HALTED", nodata: "NO DATA",
};

function Disconnected() {
  return (
    <div className="banner"><span className="dot r" aria-hidden="true" />
      Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No risk values are fabricated.</div>
  );
}

export function RiskControlCenter({ connected }: { connected: boolean }) {
  const [status, setStatus] = useState<RiskStatus | null>(null);
  const [config, setConfig] = useState<RiskConfigView | null>(null);
  const [events, setEvents] = useState<RiskEvents | null>(null);
  const [loaded, setLoaded] = useState(false);

  // An aborted fetch (StrictMode double-invoke / unmount) must NOT clobber good data with null — only
  // a real backend failure sets NO DATA. Abort rejections are ignored (matches the terminal panels).
  const loadConfigAndEvents = useCallback((signal?: AbortSignal) => {
    fetchRiskConfig(signal).then(setConfig).catch((e: any) => { if (e?.name !== "AbortError") setConfig(null); });
    fetchRiskEvents(50, signal).then(setEvents).catch((e: any) => { if (e?.name !== "AbortError") setEvents(null); });
  }, []);

  // Poll the live status; config + events are refreshed on mount and after a successful save.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const tick = () => fetchRiskStatus(ctrl.signal)
      .then((s) => { if (!cancelled) { setStatus(s); setLoaded(true); } })
      .catch((e: any) => { if (!cancelled && e?.name !== "AbortError") { setStatus(null); setLoaded(true); } });
    tick();
    loadConfigAndEvents(ctrl.signal);
    const id = setInterval(tick, 5000);
    return () => { cancelled = true; ctrl.abort(); clearInterval(id); };
  }, [loadConfigAndEvents]);

  const st = hasRiskState(status) ? (status!.status as string) : null;
  const tone = stateTone(st);
  const cur = status?.capital.currency ?? config?.config?.currency ?? null;
  const dp = status?.daily_pnl;
  const cfg = config?.config ?? null;

  return (
    <>
      {!connected ? <Disconnected /> : null}

      {/* ---- state header ---- */}
      <div className={`riskstate ${tone}`}>
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7Z" /><path d="M9 12l2 2 4-4" /></svg>
        <div>
          <div className="label">Capital Protection</div>
          <div className="hi">{loaded ? (st ?? NO_DATA) : "…"}{st ? <span className="rs-sub"> · {STATE_TITLE[tone]}</span> : null}</div>
        </div>
        <div className="rs-meta" style={{ marginLeft: "auto" }}>
          <span>Kill Switch <b className={status?.kill_switch === "STOPPED" ? "down" : status?.kill_switch === "ARMED" ? "up" : ""}>
            {status?.kill_switch ?? NO_DATA}</b></span>
          <span>Config&nbsp;v<b>{status?.configuration_version ?? config?.configuration_version ?? NO_DATA}</b></span>
        </div>
      </div>

      {/* ---- reasons (why this state) ---- */}
      {hasRiskState(status) && (status!.reasons.length || status!.missing.length) ? (
        <div className={`riskreasons ${tone}`}>
          {status!.reasons.map((rc) => <span className="rr" key={rc}>{tone === "blocked" ? "⛔" : tone === "warning" ? "⚠" : "•"} {reasonLabel(rc)}</span>)}
          {st === "NO DATA" && status!.missing.length
            ? <span className="rr-missing">Missing: {status!.missing.join(", ")} — shown as NO DATA, never assumed zero.</span>
            : null}
        </div>
      ) : null}

      <div className="grid k2">
        {/* ---- daily loss budget gauge ---- */}
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>Daily Loss Budget</h3>
          <div className="riskgauge">
            <GaugeArc frac={usedFrac(dp?.used_pct)} sub="USED"
              color={tone === "blocked" ? "var(--neg)" : tone === "warning" ? "var(--warn)" : "var(--accent)"} />
          </div>
          <div className="autobox" style={{ marginTop: 10 }}>
            <div className="autocell"><div className="label">Loss Limit</div><div className="v num">{moneyIn(dp?.limit, cur)}</div></div>
            <div className="autocell"><div className="label">Loss Today</div><div className="v num">{moneyIn(dp?.value == null ? null : Math.max(0, -(dp!.value)), cur)}</div></div>
            <div className="autocell"><div className="label">Remaining</div><div className={`v num ${dp?.remaining == null ? "" : dp.remaining < 0 ? "down" : "up"}`}>{moneyIn(dp?.remaining, cur)}</div></div>
          </div>
        </div>

        {/* ---- limits vs live usage ---- */}
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Limits &amp; Live Usage</h3>
          <div className="risklimits">
            <div className="rl"><span className="label">Trading Capital</span>
              <span className="rl-v num">{moneyIn(status?.capital.value ?? cfg?.capital ?? null, cur)}</span></div>
            <div className="rl"><span className="label">Max Per-Trade Risk</span>
              <span className="rl-v num">{pctNum(status?.position_risk.limit ?? cfg?.max_position_risk_pct)}</span></div>
            <div className="rl"><span className="label">Max Daily Loss</span>
              <span className="rl-v num">{pctNum(cfg?.max_daily_loss_pct)}<span className="rl-sub"> · {moneyIn(cfg?.max_daily_loss_amount, cur)}</span></span></div>
            <div className="rl"><span className="label">Portfolio Exposure</span>
              <span className="rl-v num">{pctNum(status?.exposure.gross_pct)} <span className="rl-sub">/ {pctNum(status?.exposure.limit_pct ?? cfg?.max_portfolio_exposure_pct)}</span></span></div>
            <div className="rl"><span className="label">Drawdown</span>
              <span className="rl-v num">{pctNum(status?.drawdown.value_pct)} <span className="rl-sub">/ {pctNum(status?.drawdown.limit_pct ?? cfg?.max_drawdown_pct)}</span></span></div>
            <div className="rl"><span className="label">Warning Threshold</span>
              <span className="rl-v num">{pctNum(cfg?.warning_threshold_pct)}</span></div>
          </div>
          {config && !config.configured ? (
            <p className="rl-nd">No complete risk configuration yet — set your limits below. Until then the state is <b>NO DATA</b> (never READY).</p>
          ) : null}
        </div>
      </div>

      {/* ---- config editor ---- */}
      <RiskConfigForm view={config} connected={connected} onSaved={() => loadConfigAndEvents()} />

      {/* ---- immutable audit trail ---- */}
      <div className="card">
        <h3 style={{ marginBottom: 6 }}>Risk Event History</h3>
        <p className="rl-sub" style={{ marginBottom: 12 }}>Immutable audit trail — configuration changes and kill-switch actions. Read-only.</p>
        {events && events.events.length ? (
          <div className="riskevents">
            {events.events.map((e, i) => (
              <div className={`re ${severityTone(e.severity)}`} key={`${e.id}-${i}`}>
                <span className="re-dot" aria-hidden="true" />
                <div className="re-body">
                  <div className="re-head"><b>{eventTitle(e.event_type)}</b>
                    {e.configuration_version != null ? <span className="re-ver">v{e.configuration_version}</span> : null}
                    <span className="re-src">{e.source === "kill_switch_audit" ? "kill switch" : "config"}</span></div>
                  {e.description ? <div className="re-desc">{e.description}</div> : null}
                  <div className="re-ts num">{e.timestamp ? e.timestamp.replace("T", " ").slice(0, 19) : NO_DATA}</div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="nodata"><div className="nd">{NO_DATA}</div>
            <p>No risk events recorded yet. Configuration changes and kill-switch actions appear here.</p></div>
        )}
      </div>
    </>
  );
}
