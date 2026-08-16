"use client";
// Traders tab (§ Phase G2.5). Quality-weighted trader consensus for the symbol: LONG/SHORT/NEUTRAL
// shares, a BULLISH/BEARISH/NEUTRAL verdict, and the top contributors ranked by quality. Loading →
// spinner text; error or no coverage → NO DATA. Intelligence signal only — never copy-trading, never
// execution, never a fabricated trader.
import React, { useEffect, useState } from "react";
import { fetchTraders } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { consensusTone, directionTone, hasTraderData, qualityTier, type TraderConsensus } from "@/lib/traders";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div>;
}

function Share({ label, pct, tone }: { label: string; pct: number | null; tone: string }) {
  const v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <div className="trshare">
      <div className="trshare-top"><span className={`trdir ${tone}`}>{label}</span><b className="num">{pct == null ? NO_DATA : `${pct}%`}</b></div>
      <div className="trbar"><i className={tone} style={{ width: `${v}%` }} /></div>
    </div>
  );
}

export function TradersList({ data, loading, error, symbol }: {
  data: TraderConsensus | null; loading: boolean; error: string | null; symbol: string;
}) {
  if (loading) return <Box title="LOADING" note="Fetching trader intelligence…" />;
  if (error) return <Box title={NO_DATA} note="Trader intelligence unavailable — nothing is shown, nothing invented." />;
  if (!hasTraderData(data))
    return <Box title={NO_DATA} note="No professional-trader coverage for this symbol yet. Consensus appears once a licensed trader-intelligence provider is connected — never fabricated." />;

  const d = data as TraderConsensus;
  return (
    <div className="traders">
      <div className="trhead">
        <div><div className="label">{symbol} Trader Intelligence</div><div className="trsub">Professional traders · quality-weighted</div></div>
        <div className="trverdict">
          <span className={`consb ${consensusTone(d.consensus)}`}>{d.consensus || NO_DATA}</span>
          <span className="trscore num">{d.weighted_score == null ? NO_DATA : `${d.weighted_score}`}<small> / 100</small></span>
        </div>
      </div>

      <div className="trshares">
        <Share label="LONG" pct={d.long_percent} tone="pos" />
        <Share label="SHORT" pct={d.short_percent} tone="neg" />
        <Share label="NEUTRAL" pct={d.neutral_percent} tone="neu" />
      </div>

      <div className="trcontrib">
        <div className="label" style={{ marginBottom: 8 }}>Top Contributors · {d.contributor_count}</div>
        {d.contributors.slice(0, 8).map((c) => (
          <div className="trrow" key={c.id}>
            <div className="trrow-main">
              <span className="trname">{c.name}</span>
              <span className={`trdir ${directionTone(c.direction)}`}>{(c.direction || "—").toUpperCase()}</span>
            </div>
            <div className="trrow-meta">
              <span className={`qb ${qualityTier(c.quality)}`}>Quality {c.quality == null ? NO_DATA : `${c.quality}/100`}</span>
              <span className="trstrat">{c.strategy || c.market_focus || "—"}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TradersFeed({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: TraderConsensus | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchTraders(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <TradersList data={state.data} loading={state.loading} error={state.error} symbol={symbol} />;
}
