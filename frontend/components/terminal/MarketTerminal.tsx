"use client";
// Market Intelligence Terminal (§ Phase UX-1) — institutional command-center IA for /markets/[symbol].
// Order (critical decision context first, minimal scrolling): symbol header → primary summary row
// (AI · Risk · Macro · Governance · Data Completeness) → research tab bar → compact chart (~300–350px)
// beside the fixed AI explanation panel → compact intelligence-card grid → detailed selected-tab content.
//
// Every domain is fetched ONCE here (no duplicate fetching) and fed to BOTH the compact cards and the
// detail panes (the pure *List components). Candles/indicators come from the durable OHLC endpoint via
// the same-origin proxy; nothing is fabricated — missing data renders NO DATA, never zero. Read-only:
// no trade/order control, EXECUTION stays explicitly disabled, the kill switch is never mutated here.
import React, { useEffect, useState } from "react";
import type { Snapshot } from "@/lib/types";
import { instrumentRef } from "@/lib/instruments";
import { symbolQuote, type Quote } from "@/lib/market";
import {
  fetchOhlc, fetchTraders, fetchFundamentals, fetchOptions, fetchConsensus, fetchMacroContext,
  fetchInstitutionalFlow, fetchRiskStatus, fetchNews, fetchGovernance, fetchCompleteness,
} from "@/lib/api";
import { NO_DATA, isPresent, price, num, spread as fmtSpread } from "@/lib/format";
import { humanStatus } from "@/lib/errors";
import type { OhlcBar } from "@/lib/ohlc";
import type { NewsItem } from "@/lib/news";
import { Dot } from "@/components/ui";
import { TerminalHeader } from "./TerminalHeader";
import { DataQuality } from "./DataQuality";
import { MarketChart } from "./MarketChart";
import { AiAnalysisPanel } from "./AiAnalysisPanel";
import { AiSummary } from "./AiSummary";
import { MacroContextCard } from "./MacroContextCard";
import { RiskCard } from "./RiskCard";
import {
  GovernanceCard, CompletenessCard, NewsCard, FundamentalsCard, OptionsCard, TradersCard,
  InstitutionalCard, InsiderClusterCard,
} from "./IntelCards";
import { NewsList } from "./NewsFeed";
import { FundamentalsList } from "./FundamentalsFeed";
import { OptionsList } from "./OptionsFeed";
import { TradersList } from "./TradersFeed";
import { AiHistoryFeed } from "./AiHistoryFeed";

type FetchState<T> = { data: T | null; loading: boolean; error: string | null };

// One symbol-keyed fetch per domain. Abort on unmount/symbol change; an AbortError never clobbers data.
function useDomainFetch<T>(fetcher: (signal: AbortSignal) => Promise<T>, key: string): FetchState<T> {
  const [state, setState] = useState<FetchState<T>>({ data: null, loading: true, error: null });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetcher(ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

const TABS = ["Overview", "News", "Fundamentals", "Options", "Traders", "AI History"] as const;
type Tab = (typeof TABS)[number];

function OverviewPane({ quote }: { quote: Quote | null }) {
  if (!quote) return <div className="ndbox"><div className="nd">{NO_DATA}</div><p>No quote for this instrument yet.</p></div>;
  const cells: [string, React.ReactNode][] = [
    ["Last", price(quote.last)],
    ["Bid / Ask", `${price(quote.bid)} / ${price(quote.ask)}`],
    ["Spread", fmtSpread(quote.bid, quote.ask)],
    ["Volume", isPresent(quote.volume) ? num(quote.volume, 0) : NO_DATA],
    ["Source", quote.source || NO_DATA],
    ["Latency", isPresent(quote.latency) ? Math.round(quote.latency as number) + "ms" : NO_DATA],
    ["Feed", quote.realtime ? "Realtime" : "Delayed"],
    ["Status", humanStatus(quote.status)],
  ];
  return <div className="ov">{cells.map(([k, v]) => <div className="ovc" key={k}><div className="label">{k}</div><div className="v num">{v}</div></div>)}</div>;
}

export function MarketTerminal({ s, symbol, connected }: { s: Snapshot | null; symbol: string; connected: boolean }) {
  const refData = instrumentRef(symbol);
  const quote = symbolQuote(s, symbol);
  const decisions = (s?.autonomous?.decisions || []).filter(
    (d: any) => (d.instrument || d.symbol || "").toUpperCase() === symbol.toUpperCase());
  const dec = decisions[0] || null;
  const ai = dec ? { action: dec.action, entry: dec.entry, stop: dec.stop, target: dec.target } : null;

  const [iv, setIv] = useState("1m");
  const [ohlc, setOhlc] = useState<{ bars: OhlcBar[] | null; loading: boolean; error: string | null }>({ bars: null, loading: true, error: null });
  const [tab, setTab] = useState<Tab>("Overview");

  // OHLC has its own interval dependency; the rest are symbol-keyed. All fetched ONCE, reused everywhere.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setOhlc((o) => ({ bars: o.bars, loading: true, error: null }));
    fetchOhlc(symbol, iv, 500, ctrl.signal)
      .then((r) => { if (!cancelled) setOhlc({ bars: r.bars, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setOhlc({ bars: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol, iv]);

  const consensus = useDomainFetch((sig) => fetchConsensus(symbol, sig), symbol);
  const traders = useDomainFetch((sig) => fetchTraders(symbol, sig), symbol);
  const fundamentals = useDomainFetch((sig) => fetchFundamentals(symbol, sig), symbol);
  const options = useDomainFetch((sig) => fetchOptions(symbol, sig), symbol);
  const macro = useDomainFetch((sig) => fetchMacroContext(symbol, sig), symbol);
  const institutional = useDomainFetch((sig) => fetchInstitutionalFlow(symbol, sig), symbol);
  const risk = useDomainFetch((sig) => fetchRiskStatus(sig), symbol);
  const news = useDomainFetch<{ symbol: string; items: NewsItem[] }>((sig) => fetchNews(symbol, 30, sig), symbol);
  const governance = useDomainFetch((sig) => fetchGovernance(symbol, sig), symbol);
  const completeness = useDomainFetch((sig) => fetchCompleteness(symbol, sig), symbol);

  const bars = ohlc.bars || [];
  const change = bars.length > 1 ? (bars[bars.length - 1].close - bars[0].close) / (bars[0].close || 1) : null;

  const convictionInputs = [
    { label: "Price Action", value: null as number | null },
    { label: "News", value: null as number | null },
    { label: "Fundamentals", value: fundamentals.data?.quality_score ?? null },
    { label: "Options Flow", value: options.data?.options_score ?? null },
    { label: "Trader Consensus", value: traders.data?.weighted_score ?? null },
    { label: "Institutional Flow", value: institutional.data?.accumulation_score ?? null },
    { label: "Insider Activity", value: institutional.data?.insider_score ?? null },
    { label: "Insider Cluster", value: institutional.data?.insider_cluster?.score ?? null },
    { label: "Macro", value: macro.data?.score ?? null },
    { label: "Risk", value: null as number | null },
  ];

  const open = (t: Tab) => () => setTab(t);

  return (
    <div className="term">
      {!connected ? (
        <div className="banner"><Dot tone="r" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No values are fabricated.</div>
      ) : null}

      <TerminalHeader symbol={symbol} refData={refData} quote={quote} mode={s?.mode} change={change} connected={connected} />

      {/* ── primary summary row: the 5-second decision context ── */}
      <div className="summary-row">
        <AiSummary data={consensus.data} />
        <RiskCard data={risk.data} />
        <MacroContextCard data={macro.data} />
        <GovernanceCard data={governance.data} />
        <CompletenessCard data={completeness.data} />
      </div>

      {/* ── research tab bar (moved directly below the summary) ── */}
      <div className="tabs" role="tablist" aria-label="Research tabs">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {/* ── workbench (§ UX-1.1): two independently-flowing columns. Flat DOM order — chart, AI, intel,
          detail — so mobile stacks them in that semantic order; on desktop CSS grid-areas place the
          chart/intel/detail in the LEFT column and the AI panel in the RIGHT column (sticky + bounded,
          internal scroll). The AI panel no longer determines the left column's height, so the chart is
          never stretched and the intelligence cards start immediately below it. ── */}
      <div className="workbench">
        <div className="card term-chart compact wb-chart">
          <MarketChart bars={ohlc.bars} loading={ohlc.loading} error={ohlc.error} interval={iv} onInterval={setIv} ai={ai} compact />
        </div>

        <div className="wb-ai">
          <AiAnalysisPanel dec={dec} risk={s?.trading_risk || null} mode={s?.mode} executionEnabled={s?.execution_enabled}
            convictionInputs={convictionInputs} consensus={consensus.data} governance={governance.data}
            riskStatus={risk.data?.status ?? null} completeness={completeness.data} />
        </div>

        {/* compact intelligence-card grid — begins directly below the chart in the left column */}
        <div className="intel-grid wb-intel">
          <NewsCard items={news.data?.items ?? null} onOpen={open("News")} />
          <FundamentalsCard data={fundamentals.data} onOpen={open("Fundamentals")} />
          <OptionsCard data={options.data} onOpen={open("Options")} />
          <TradersCard data={traders.data} onOpen={open("Traders")} />
          <InstitutionalCard data={institutional.data} />
          <InsiderClusterCard data={institutional.data} />
          <div className="icard neu"><div className="ic-head"><span className="label">Market Data Quality</span></div>
            <div className="ic-body ic-dq"><DataQuality quote={quote} refData={refData} /></div></div>
        </div>

        {/* detailed selected-tab content (reuses the same fetched data — no re-fetch) */}
        <div className="card wb-detail" id="term-detail">
          {tab === "Overview" && <OverviewPane quote={quote} />}
          {tab === "News" && <NewsList items={news.data?.items ?? null} loading={news.loading} error={news.error} />}
          {tab === "Fundamentals" && <FundamentalsList data={fundamentals.data} loading={fundamentals.loading} error={fundamentals.error} symbol={symbol} />}
          {tab === "Options" && <OptionsList data={options.data} loading={options.loading} error={options.error} symbol={symbol} />}
          {tab === "Traders" && <TradersList data={traders.data} loading={traders.loading} error={traders.error} symbol={symbol} />}
          {tab === "AI History" && <AiHistoryFeed symbol={symbol} />}
        </div>
      </div>
    </div>
  );
}
