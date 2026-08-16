"use client";
// Research tabs: Overview · News · Fundamentals · Options · AI History. News/Fundamentals/Options have
// no feed yet → NO DATA (never a placeholder pretending to be real). AI History uses the EventTimeline.
import React, { useState } from "react";
import { NO_DATA, price, num, spread as fmtSpread, isPresent } from "@/lib/format";
import { humanStatus } from "@/lib/errors";
import { NoData } from "@/components/ui";
import { EventTimeline, type TimelineEvent } from "./EventTimeline";
import { NewsFeed } from "./NewsFeed";
import { TradersFeed } from "./TradersFeed";
import { FundamentalsFeed } from "./FundamentalsFeed";
import { OptionsFeed } from "./OptionsFeed";
import type { Quote } from "@/lib/market";

const TABS = ["Overview", "News", "Fundamentals", "Options", "Traders", "AI History"] as const;
type Tab = (typeof TABS)[number];

function Overview({ quote }: { quote: Quote | null }) {
  if (!quote) return <NoData note="No quote for this instrument yet." />;
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
  return (
    <div className="ov">
      {cells.map(([k, v]) => <div className="ovc" key={k}><div className="label">{k}</div><div className="v num">{v}</div></div>)}
    </div>
  );
}

function NoFeed({ note }: { note: string }) {
  return <div className="ndbox"><div className="nd">{NO_DATA}</div><p>{note}</p></div>;
}

export function ResearchTabs({ quote, decisions, symbol }: { quote: Quote | null; decisions: Record<string, any>[]; symbol: string }) {
  const [tab, setTab] = useState<Tab>("Overview");
  const aiEvents: TimelineEvent[] = (decisions || []).map((d) => {
    const verdict = (d.risk_decision || "").toString().toUpperCase();
    const action = (d.action || "").toString().toUpperCase();
    return {
      ts: d.ts, kind: "signal", title: `${action || "—"} ${d.instrument || symbol}`, detail: d.reason,
      tone: verdict === "APPROVED" ? "ok" : verdict === "REJECTED" ? "sell" : action === "SELL" ? "sell" : action === "BUY" ? "buy" : "muted",
    };
  });
  return (
    <div className="card">
      <div className="tabs">
        {TABS.map((t) => <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}
      </div>
      {tab === "Overview" && <Overview quote={quote} />}
      {tab === "News" && <NewsFeed symbol={symbol} />}
      {tab === "Fundamentals" && <FundamentalsFeed symbol={symbol} />}
      {tab === "Options" && <OptionsFeed symbol={symbol} />}
      {tab === "Traders" && <TradersFeed symbol={symbol} />}
      {tab === "AI History" && <EventTimeline events={aiEvents} />}
    </div>
  );
}
