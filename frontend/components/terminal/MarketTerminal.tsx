"use client";
// Market Intelligence Terminal — composes the header, interactive chart, AI analysis panel and research
// tabs for /markets/[symbol]. Pure sub-components do the rendering; this is the wiring. Frontend only.
import React from "react";
import type { Snapshot } from "@/lib/types";
import { instrumentRef } from "@/lib/instruments";
import { symbolQuote } from "@/lib/market";
import { ohlcForSymbol } from "@/lib/ohlc";
import { Dot } from "@/components/ui";
import { TerminalHeader } from "./TerminalHeader";
import { DataQuality } from "./DataQuality";
import { MarketChart } from "./MarketChart";
import { AiAnalysisPanel } from "./AiAnalysisPanel";
import { ResearchTabs } from "./ResearchTabs";

export function MarketTerminal({ s, symbol, connected }: { s: Snapshot | null; symbol: string; connected: boolean }) {
  const refData = instrumentRef(symbol);
  const quote = symbolQuote(s, symbol);
  const decisions = (s?.autonomous?.decisions || []).filter(
    (d: any) => (d.instrument || d.symbol || "").toUpperCase() === symbol.toUpperCase());
  const dec = decisions[0] || null;
  const series = ohlcForSymbol(s, symbol);
  const change = series && series.bars.length > 1
    ? (series.bars[series.bars.length - 1].close - series.bars[0].close) / (series.bars[0].close || 1)
    : null;
  const ai = dec ? { action: dec.action, entry: dec.entry, stop: dec.stop, target: dec.target } : null;

  return (
    <div className="term">
      {!connected ? (
        <div className="banner"><Dot tone="r" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No values are fabricated.</div>
      ) : null}
      <TerminalHeader symbol={symbol} refData={refData} quote={quote} mode={s?.mode} change={change} connected={connected} />
      <DataQuality quote={quote} refData={refData} />
      <div className="term-main">
        <div className="card term-chart"><MarketChart series={series} ai={ai} /></div>
        <AiAnalysisPanel dec={dec} risk={s?.trading_risk || null} mode={s?.mode} executionEnabled={s?.execution_enabled} />
      </div>
      <ResearchTabs quote={quote} decisions={decisions} symbol={symbol} />
    </div>
  );
}
