"use client";
// Compact, always-visible intelligence cards (§ Phase UX-1) for the Market Terminal. Each is PURE and
// fed by data the terminal already fetched (no duplicate fetching). Every card shows a status/score, the
// key signal and one principal risk/conflict, with an honest NO DATA state — never a fabricated value.
// Clicking a card that has a detail tab selects that tab (via onOpen); nothing here trades or executes.
import React from "react";
import { NO_DATA } from "@/lib/format";
import { hasGovernance, govTone, reasonText, type Governance } from "@/lib/governance";
import { hasCompleteness, type Completeness } from "@/lib/completeness";
import { isValidNewsItem, sentimentTone as newsSentTone, type NewsItem } from "@/lib/news";
import { hasFundamentals, valuationLabel, type FundamentalsData } from "@/lib/fundamentals";
import { hasOptions, sentimentTone as optSentTone, ivPct, type OptionsData } from "@/lib/options";
import { hasTraderData, consensusTone, type TraderConsensus } from "@/lib/traders";
import {
  hasInstitutional, flowTone, insiderTone, clusterTone, clusterLabel, type InstitutionalFlow,
} from "@/lib/institutional";

type Tone = "pos" | "neg" | "warn" | "neu";

/** Shared compact-card shell. Renders as a focusable button when `onOpen` is given (keyboard-accessible). */
function IntelCard({ title, tone = "neu", badge, onOpen, children, note }: {
  title: string; tone?: Tone; badge?: React.ReactNode; onOpen?: () => void;
  children?: React.ReactNode; note?: string;
}) {
  const inner = (
    <>
      <div className="ic-head"><span className="label">{title}</span>{badge}{onOpen ? <span className="ic-open" aria-hidden="true">→</span> : null}</div>
      {children ? <div className="ic-body">{children}</div> : <div className="ic-nd">{NO_DATA}</div>}
      {note ? <div className="ic-note">{note}</div> : null}
    </>
  );
  const cls = `icard ${tone}${onOpen ? " icard-btn" : ""}`;
  return onOpen
    ? <button type="button" className={cls} onClick={onOpen} aria-label={`${title} — open detail`}>{inner}</button>
    : <div className={cls}>{inner}</div>;
}

function Badge({ tone, children }: { tone: string; children: React.ReactNode }) {
  return <span className={`ic-badge ${tone}`}>{children}</span>;
}
function NDBadge() { return <span className="ic-ndb">{NO_DATA}</span>; }

// ---- Governance -----------------------------------------------------------
export function GovernanceCard({ data, onOpen }: { data: Governance | null; onOpen?: () => void }) {
  if (!hasGovernance(data)) return <IntelCard title="Governance" badge={<NDBadge />} onOpen={onOpen} />;
  const g = data as Governance;
  return (
    <IntelCard title="Governance" tone={g.status === "APPROVED" ? "pos" : g.status === "BLOCKED" ? "neg" : "warn"}
      badge={<Badge tone={govTone(g.status)}>{g.status}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Approved</span><b className="num">{g.approved ? "Yes" : "No"}</b></div>
      <div className="ic-metric"><span className="k">Completeness</span><b className="num">{g.data_completeness == null ? NO_DATA : `${g.data_completeness}%`}</b></div>
      <div className="ic-sig">{g.reasons.length ? reasonText(g.reasons[0]) : (g.approved ? "All governance rules satisfied" : "—")}</div>
    </IntelCard>
  );
}

// ---- Data Completeness ----------------------------------------------------
export function CompletenessCard({ data, onOpen }: { data: Completeness | null; onOpen?: () => void }) {
  if (!hasCompleteness(data)) return <IntelCard title="Data Completeness" badge={<NDBadge />} onOpen={onOpen} />;
  const c = data as Completeness;
  const tone = c.state === "READY" ? "pos" : c.state === "PARTIAL" ? "warn" : "neg";
  return (
    <IntelCard title="Data Completeness" tone={tone}
      badge={<Badge tone={tone}>{c.state}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Coverage</span><b className="num">{c.score == null ? NO_DATA : `${c.score}/100`}</b></div>
      <div className="ic-metric"><span className="k">Sources</span><b className="num">{c.available.length}✓ · {c.missing.length}✗</b></div>
      <div className="ic-sig">{c.missing.length ? `Missing: ${c.missing.slice(0, 2).join(", ")}` : "All sources available"}</div>
    </IntelCard>
  );
}

// ---- News -----------------------------------------------------------------
export function NewsCard({ items, onOpen }: { items: NewsItem[] | null; onOpen?: () => void }) {
  const clean = (items || []).filter(isValidNewsItem);
  if (clean.length === 0) return <IntelCard title="News" badge={<NDBadge />} onOpen={onOpen} />;
  const top = clean[0];
  const pos = clean.filter((n) => n.sentiment === "positive").length;
  const neg = clean.filter((n) => n.sentiment === "negative").length;
  return (
    <IntelCard title="News" tone={neg > pos ? "neg" : pos > neg ? "pos" : "neu"}
      badge={<Badge tone={newsSentTone(top.sentiment)}>{clean.length} items</Badge>} onOpen={onOpen}>
      <div className="ic-sig ic-clip">{top.title}</div>
      <div className="ic-metric"><span className="k">Sentiment</span><b className="num">{pos}+ / {neg}−</b></div>
      <div className="ic-note">{top.source || "—"}</div>
    </IntelCard>
  );
}

// ---- Fundamentals ---------------------------------------------------------
export function FundamentalsCard({ data, onOpen }: { data: FundamentalsData | null; onOpen?: () => void }) {
  if (!hasFundamentals(data)) return <IntelCard title="Fundamentals" badge={<NDBadge />} onOpen={onOpen} />;
  const f = data as FundamentalsData;
  const q = f.quality_score;
  const tone = q == null ? "neu" : q >= 70 ? "pos" : q >= 45 ? "warn" : "neg";
  return (
    <IntelCard title="Fundamentals" tone={tone}
      badge={<Badge tone={tone}>{q == null ? NO_DATA : `${q}/100`}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Valuation</span><b className="num">{valuationLabel(f.valuation?.pe_ratio)}</b></div>
      <div className="ic-metric"><span className="k">Rating</span><b className="num">{f.analyst_estimates?.rating ?? NO_DATA}</b></div>
      <div className="ic-sig">{f.strengths[0] ? `✓ ${f.strengths[0]}` : f.risks[0] ? `⚠ ${f.risks[0]}` : "—"}</div>
    </IntelCard>
  );
}

// ---- Options --------------------------------------------------------------
export function OptionsCard({ data, onOpen }: { data: OptionsData | null; onOpen?: () => void }) {
  if (!hasOptions(data)) return <IntelCard title="Options" badge={<NDBadge />} onOpen={onOpen} />;
  const o = data as OptionsData;
  const tone = o.sentiment === "Bullish" ? "pos" : o.sentiment === "Bearish" ? "neg" : "neu";
  return (
    <IntelCard title="Options" tone={tone}
      badge={<Badge tone={optSentTone(o.sentiment)}>{o.sentiment ?? NO_DATA}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Score</span><b className="num">{o.options_score == null ? NO_DATA : `${o.options_score}/100`}</b></div>
      <div className="ic-metric"><span className="k">Impl. Vol</span><b className="num">{ivPct(o.implied_volatility)}</b></div>
      <div className="ic-sig">{o.unusual_activity === "Detected" ? "⚠ Unusual activity detected" : o.signals[0] || "—"}</div>
    </IntelCard>
  );
}

// ---- Trader / SEC 13F ------------------------------------------------------
export function TradersCard({ data, onOpen }: { data: TraderConsensus | null; onOpen?: () => void }) {
  if (!hasTraderData(data)) return <IntelCard title="Traders · 13F" badge={<NDBadge />} onOpen={onOpen} />;
  const t = data as TraderConsensus;
  const tone = t.consensus === "BULLISH" ? "pos" : t.consensus === "BEARISH" ? "neg" : "neu";
  return (
    <IntelCard title="Traders · 13F" tone={tone}
      badge={<Badge tone={consensusTone(t.consensus)}>{t.consensus ?? NO_DATA}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Weighted</span><b className="num">{t.weighted_score == null ? NO_DATA : `${Math.round(t.weighted_score)}/100`}</b></div>
      <div className="ic-metric"><span className="k">Contributors</span><b className="num">{t.contributor_count}</b></div>
      <div className="ic-sig">{t.long_percent != null ? `${Math.round(t.long_percent)}% long · ${Math.round(t.short_percent ?? 0)}% short` : "—"}</div>
    </IntelCard>
  );
}

// ---- Institutional Flow ----------------------------------------------------
export function InstitutionalCard({ data, onOpen }: { data: InstitutionalFlow | null; onOpen?: () => void }) {
  if (!hasInstitutional(data)) return <IntelCard title="Institutional Flow" badge={<NDBadge />} onOpen={onOpen} />;
  const f = data as InstitutionalFlow;
  return (
    <IntelCard title="Institutional Flow" tone={flowTone(f.institutional_direction) === "acc" ? "pos" : flowTone(f.institutional_direction) === "red" ? "neg" : "warn"}
      badge={<Badge tone={flowTone(f.institutional_direction)}>{f.institutional_direction ?? NO_DATA}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Accumulation</span><b className="num">{f.accumulation_score == null ? NO_DATA : `${f.accumulation_score}/100`}</b></div>
      <div className="ic-metric"><span className="k">Insider</span><b className={`num ${insiderTone(f.insider_sentiment)}`}>{f.insider_sentiment ?? NO_DATA}</b></div>
      <div className="ic-sig">{f.institutional_changes.length} 13F changes · {f.insider_summary.buy_count}B/{f.insider_summary.sell_count}S insider tx</div>
    </IntelCard>
  );
}

// ---- Insider Cluster -------------------------------------------------------
export function InsiderClusterCard({ data, onOpen }: { data: InstitutionalFlow | null; onOpen?: () => void }) {
  const cl = data?.insider_cluster;
  if (!cl || cl.cluster_type == null) return <IntelCard title="Insider Cluster" badge={<NDBadge />} onOpen={onOpen} />;
  const tone = clusterTone(cl.cluster_type);
  return (
    <IntelCard title="Insider Cluster" tone={tone === "acc" ? "pos" : tone === "red" ? "neg" : "neu"}
      badge={<Badge tone={tone}>{clusterLabel(cl.cluster_type)}</Badge>} onOpen={onOpen}>
      <div className="ic-metric"><span className="k">Score</span><b className="num">{cl.score == null ? NO_DATA : `${cl.score}/100`}</b></div>
      <div className="ic-metric"><span className="k">Insiders</span><b className="num">{cl.insider_count}</b></div>
      <div className="ic-sig ic-clip">{cl.summary ?? "—"}</div>
    </IntelCard>
  );
}
