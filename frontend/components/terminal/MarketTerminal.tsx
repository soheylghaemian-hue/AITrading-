"use client";
// Market Intelligence Terminal — composes the header, interactive chart, AI analysis panel and research
// tabs for /markets/[symbol]. Pure sub-components do the rendering; this is the wiring. Frontend only.
// Candles come from the durable OHLC endpoint (GET /market/{symbol}/ohlc) reached through the same-origin
// server proxy — never the browser holding a token, never a parallel API client. On no data / error the
// chart shows NO DATA; nothing is fabricated.
import React, { useEffect, useState } from "react";
import type { Snapshot } from "@/lib/types";
import { instrumentRef } from "@/lib/instruments";
import { symbolQuote } from "@/lib/market";
import { fetchOhlc, fetchTraders, fetchFundamentals, fetchOptions, fetchConsensus, fetchMacroContext } from "@/lib/api";
import type { OhlcBar } from "@/lib/ohlc";
import type { TraderConsensus } from "@/lib/traders";
import type { FundamentalsData } from "@/lib/fundamentals";
import type { OptionsData } from "@/lib/options";
import type { AiConsensus } from "@/lib/consensus";
import type { MacroContext } from "@/lib/macro";
import { Dot } from "@/components/ui";
import { TerminalHeader } from "./TerminalHeader";
import { DataQuality } from "./DataQuality";
import { MarketChart } from "./MarketChart";
import { AiAnalysisPanel } from "./AiAnalysisPanel";
import { AiSummary } from "./AiSummary";
import { MacroContextCard } from "./MacroContextCard";
import { ResearchTabs } from "./ResearchTabs";

type OhlcState = { bars: OhlcBar[] | null; loading: boolean; error: string | null };

export function MarketTerminal({ s, symbol, connected }: { s: Snapshot | null; symbol: string; connected: boolean }) {
  const refData = instrumentRef(symbol);
  const quote = symbolQuote(s, symbol);
  const decisions = (s?.autonomous?.decisions || []).filter(
    (d: any) => (d.instrument || d.symbol || "").toUpperCase() === symbol.toUpperCase());
  const dec = decisions[0] || null;
  const ai = dec ? { action: dec.action, entry: dec.entry, stop: dec.stop, target: dec.target } : null;

  const [iv, setIv] = useState("1m");
  const [ohlc, setOhlc] = useState<OhlcState>({ bars: null, loading: true, error: null });
  const [traders, setTraders] = useState<TraderConsensus | null>(null);
  const [fundamentals, setFundamentals] = useState<FundamentalsData | null>(null);
  const [options, setOptions] = useState<OptionsData | null>(null);
  const [consensus, setConsensus] = useState<AiConsensus | null>(null);
  const [macro, setMacro] = useState<MacroContext | null>(null);

  // Fetch OHLC whenever the symbol or timeframe changes. AbortController + a cancelled flag guard against
  // a stale response landing after a fast symbol switch (NVDA → AAPL → SPY) overwriting the newer request.
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

  // §G2.5: fetch the quality-weighted trader consensus and feed it to the AI Brain conviction inputs.
  // Read-only intelligence signal; null → NO DATA. Never a trading decision or copy-trade.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    fetchTraders(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setTraders(r); })
      .catch(() => { if (!cancelled) setTraders(null); });
    fetchFundamentals(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setFundamentals(r); })
      .catch(() => { if (!cancelled) setFundamentals(null); });
    fetchOptions(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setOptions(r); })
      .catch(() => { if (!cancelled) setOptions(null); });
    fetchConsensus(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setConsensus(r); })
      .catch(() => { if (!cancelled) setConsensus(null); });
    fetchMacroContext(symbol, ctrl.signal)                  // §R1.2 macro context for this symbol
      .then((r) => { if (!cancelled) setMacro(r); })
      .catch(() => { if (!cancelled) setMacro(null); });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);

  // Header change% is derived from the REAL fetched candles (first→last close), not the snapshot. No bars → NO DATA.
  const bars = ohlc.bars || [];
  const change = bars.length > 1 ? (bars[bars.length - 1].close - bars[0].close) / (bars[0].close || 1) : null;

  // AI Brain conviction inputs: each is a real 0-100 signal or NO DATA. Only Trader Consensus is wired
  // in this phase (from the quality-weighted score); the others get their feeds later. No fabricated total.
  const convictionInputs = [
    { label: "Price Action", value: null as number | null },
    { label: "News", value: null as number | null },
    { label: "Fundamentals", value: fundamentals?.quality_score ?? null },
    { label: "Options Flow", value: options?.options_score ?? null },
    { label: "Trader Consensus", value: traders?.weighted_score ?? null },
    { label: "Macro", value: macro?.score ?? null },        // §R1.2 wired to the macro environment score
    { label: "Risk", value: null as number | null },
  ];

  return (
    <div className="term">
      {!connected ? (
        <div className="banner"><Dot tone="r" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No values are fabricated.</div>
      ) : null}
      <TerminalHeader symbol={symbol} refData={refData} quote={quote} mode={s?.mode} change={change} connected={connected} />
      <AiSummary data={consensus} />
      <MacroContextCard data={macro} />
      <DataQuality quote={quote} refData={refData} />
      <div className="term-main">
        <div className="card term-chart">
          <MarketChart bars={ohlc.bars} loading={ohlc.loading} error={ohlc.error} interval={iv} onInterval={setIv} ai={ai} />
        </div>
        <AiAnalysisPanel dec={dec} risk={s?.trading_risk || null} mode={s?.mode} executionEnabled={s?.execution_enabled} convictionInputs={convictionInputs} />
      </div>
      <ResearchTabs quote={quote} decisions={decisions} symbol={symbol} />
    </div>
  );
}
