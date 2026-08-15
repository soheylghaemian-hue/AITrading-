// Presentational primitives for the Command Center. All pure (no hooks) so they render in tests via
// renderToStaticMarkup. NO DATA discipline: a missing value never becomes a fabricated 0 or price.
import React from "react";
import { NO_DATA, isPresent, money, num, price, hhmmss } from "@/lib/format";
import { translateError } from "@/lib/errors";
import type { Tone } from "@/lib/select";

export function Dot({ tone }: { tone: Tone }) {
  return <span className={`dot ${tone}`} aria-hidden="true" />;
}

export function StatusPill({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <span className="pill"><Dot tone={tone} />{label} <b>{value}</b></span>
  );
}

export function Tag({ kind, children }: { kind: string; children: React.ReactNode }) {
  return <span className={`tag ${kind}`}>{children}</span>;
}

export function NoData({ label = NO_DATA, note }: { label?: string; note?: string }) {
  return (
    <div className="nodata">
      <div className="nd">{label}</div>
      {note ? <p>{note}</p> : null}
    </div>
  );
}

/** A friendly error box; the raw code (e.g. "IBKR 10089") only appears under Details. */
export function ErrorDetail({ code, raw, info = false }: { code?: number | null; raw?: string | null; info?: boolean }) {
  const t = translateError(code, raw);
  return (
    <div className={`attn${info ? " info" : ""}`}>
      <svg className="ic" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <circle cx="12" cy="12" r="9" /><path d="M12 8v4M12 16v.3" />
      </svg>
      <div>
        <div className="t">{t.title}</div>
        {t.detail ? (
          <div className="d"><details><summary><span className="chev">▸</span>Details</summary>
            <span className="num" style={{ fontSize: 12, color: "var(--faint)" }}>{t.detail}</span></details></div>
        ) : null}
      </div>
    </div>
  );
}

/** SVG sparkline from a numeric series. Empty/absent series renders nothing (caller shows NO DATA). */
export function Sparkline({ points, color = "var(--accent)", fill }: { points?: (number | null)[]; color?: string; fill?: string }) {
  const pts = (points || []).filter(isPresent) as number[];
  if (pts.length < 2) return null;
  const w = 200, h = 34, min = Math.min(...pts), max = Math.max(...pts), span = max - min || 1;
  const d = pts.map((v, i) => `${(i / (pts.length - 1)) * w} ${h - ((v - min) / span) * (h - 4) - 2}`).join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      <path d={`M${d}`} fill="none" stroke={color} strokeWidth="1.6" />
      {fill ? <path d={`M${d} ${w} ${h} 0 ${h}Z`} fill={fill} /> : null}
    </svg>
  );
}

/** Horizontal budget meter. frac is 0..1 or null (→ NO DATA text). */
export function Meter({ frac, color = "var(--accent)", caption }: { frac: number | null; color?: string; caption?: string }) {
  if (frac === null) return <span className="num neut" style={{ fontSize: 12 }}>{NO_DATA}</span>;
  const w = Math.min(100, Math.max(0, frac * 100));
  return (
    <div className="gaugewrap">
      <div className="meterbar"><i style={{ width: `${w}%`, background: color }} /></div>
      {caption ? <span className="num" style={{ fontSize: 12, color: "var(--dim)" }}>{caption}</span> : null}
    </div>
  );
}

/** Semicircle gauge for the risk budget. */
export function GaugeArc({ frac, sub = "USED", color = "var(--accent)" }: { frac: number | null; sub?: string; color?: string }) {
  const f = frac === null ? 0 : Math.min(1, Math.max(0, frac));
  const dash = 283, off = dash - dash * f;
  return (
    <svg width="220" height="130" viewBox="0 0 220 130" role="img" aria-label={`${sub} ${frac === null ? NO_DATA : Math.round(f * 100) + "%"}`}>
      <path d="M20 120 A90 90 0 0 1 200 120" fill="none" stroke="var(--line)" strokeWidth="14" strokeLinecap="round" />
      <path d="M20 120 A90 90 0 0 1 200 120" fill="none" stroke={color} strokeWidth="14" strokeLinecap="round"
        strokeDasharray={dash} strokeDashoffset={off} />
      <text x="110" y="104" textAnchor="middle" fill="var(--ink)" fontFamily="var(--mono)" fontSize="30" fontWeight="600">
        {frac === null ? NO_DATA : Math.round(f * 100) + "%"}</text>
      <text x="110" y="122" textAnchor="middle" fill="var(--faint)" fontSize="10" letterSpacing="1.5">{sub}</text>
    </svg>
  );
}

// ---- domain cards ---------------------------------------------------------
function posSide(p: any): "long" | "short" | null {
  const q = p?.quantity ?? p?.qty;
  if (!isPresent(q) || q === 0) return null;
  return q > 0 ? "long" : "short";
}

export function PositionCard({ p }: { p: Record<string, any> }) {
  const sym = p.symbol || p.instrument || "—";
  const side = posSide(p);
  const pnl = p.unrealized_pnl ?? p.pnl;
  return (
    <div className="deccard">
      <div className="decrow">
        <span className="decsym">{sym}{side ? <> <Tag kind={side}>{side.toUpperCase()}</Tag></> : null}</span>
        <span className={`num ${isPresent(pnl) ? (pnl >= 0 ? "up" : "down") : "neut"}`} style={{ fontSize: 18, fontWeight: 600 }}>
          {isPresent(pnl) ? (pnl >= 0 ? "+" : "−") + money(Math.abs(pnl)).slice(1) : NO_DATA}
        </span>
      </div>
      <div className="decmeta">
        <div className="cell"><div className="label">Entry</div><div className="num">{price(p.avg_price ?? p.entry)}</div></div>
        <div className="cell"><div className="label">Current</div><div className="num">{price(p.market_price ?? p.current ?? p.mark)}</div></div>
        <div className="cell"><div className="label">Qty</div><div className="num">{num(p.quantity ?? p.qty, 0)}</div></div>
        <div className="cell"><div className="label">Risk</div><div className="num">{money(p.risk ?? p.monetary_risk)}</div></div>
      </div>
    </div>
  );
}

export function DecisionCard({ d, showAgents = true }: { d: Record<string, any>; showAgents?: boolean }) {
  const sym = d.instrument || d.symbol || "—";
  const action = (d.action || d.final_decision || d.decision || "").toString().toUpperCase();
  const kind = action === "BUY" ? "buy" : action === "SELL" ? "sell" : "muted";
  const conf = d.confidence;
  const verdict = (d.risk_decision || "").toString().toUpperCase();
  const vKind = verdict === "APPROVED" ? "ok" : verdict === "REJECTED" ? "rej" : "muted";
  const agents = d.agents as Record<string, any> | undefined;
  return (
    <div className="deccard">
      <div className="decrow">
        <span className="decsym">{sym} {action ? <Tag kind={kind}>{action}</Tag> : null}</span>
        <div className="conv">
          <span className="label" style={{ letterSpacing: ".5px" }}>Conviction</span>
          <div className="confbar"><i style={{ width: isPresent(conf) ? `${Math.round(conf * 100)}%` : "0%" }} /></div>
          <span className="num" style={{ fontWeight: 600 }}>{isPresent(conf) ? Math.round(conf * 100) + "%" : NO_DATA}</span>
        </div>
      </div>
      <div className="reason">{d.reason || NO_DATA}</div>
      <div className="decmeta">
        <div className="cell"><div className="label">Entry</div><div className="num">{price(d.entry)}</div></div>
        <div className="cell"><div className="label">Stop</div><div className="num" style={{ color: isPresent(d.stop) ? "var(--neg)" : undefined }}>{price(d.stop)}</div></div>
        <div className="cell"><div className="label">Target</div><div className="num" style={{ color: isPresent(d.target) ? "var(--pos)" : undefined }}>{price(d.target)}</div></div>
        <div className="cell"><div className="label">Monetary Risk</div><div className="num">{money(d.monetary_risk)}</div></div>
        <div className="cell"><div className="label">Risk Engine</div><div>{verdict ? <Tag kind={vKind}>{verdict}</Tag> : <span className="num neut">{NO_DATA}</span>}</div></div>
        <div className="cell"><div className="label">Execution</div><div><Tag kind="muted">PAPER</Tag></div></div>
      </div>
      {showAgents ? (
        <div className="why"><details><summary><span className="chev">▸</span>WHY? — agent breakdown</summary>
          <div className="agents">
            {["momentum", "value", "risk", "macro"].map((a) => {
              const v = agents?.[a];
              const label = a.charAt(0).toUpperCase() + a.slice(1) + " Agent";
              return <div className="agent" key={a}><b>{label}</b>{v != null
                ? <Tag kind="muted">{String(v)}</Tag> : <span className="num neut">{NO_DATA}</span>}</div>;
            })}
          </div>
        </details></div>
      ) : null}
    </div>
  );
}

export function DecisionTimeline({ decisions }: { decisions?: Record<string, any>[] }) {
  const rows = (decisions || []).slice(0, 12);
  if (rows.length === 0) return <NoData note="No AI decisions yet. Decisions appear here as the engine evaluates the market." />;
  return (
    <div className="timeline">
      {rows.map((d, i) => {
        const action = (d.action || d.final_decision || d.decision || "").toString().toUpperCase();
        const node = action === "BUY" ? "buy" : action === "SELL" ? "sell" : "no_data";
        const verdict = (d.risk_decision || "").toString().toUpperCase();
        const vKind = verdict === "APPROVED" ? "ok" : verdict === "REJECTED" ? "rej" : "muted";
        return (
          <div className="tl-i" key={i}>
            <div className="tl-t">{hhmmss(d.ts)}</div>
            <div className="tl-rail"><span className={`tl-node ${node}`} />{i < rows.length - 1 ? <span className="tl-line" /> : null}</div>
            <div className="tl-body">
              <div className="tl-head"><b>{d.instrument || d.symbol || "—"}</b>
                {action ? <Tag kind={node === "buy" ? "buy" : node === "sell" ? "sell" : "muted"}>{action}</Tag> : null}
                {verdict ? <Tag kind={vKind}>{verdict}</Tag> : null}</div>
              <div className="tl-reason">{d.reason || NO_DATA}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
