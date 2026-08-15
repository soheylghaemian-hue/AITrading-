// Terminal header (pure): symbol · company · exchange · price · change · session · source · realtime ·
// latency · last update · data quality. Every absent value renders NO DATA — never fabricated.
import React from "react";
import { NO_DATA, isPresent, price, pct, hhmmss } from "@/lib/format";
import { humanStatus } from "@/lib/errors";
import type { InstrumentRef } from "@/lib/instruments";
import type { Quote } from "@/lib/market";
import { Dot, Tag } from "../ui";

function M({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="thm"><div className="label">{label}</div><div className="v">{children}</div></div>;
}

export function TerminalHeader({ symbol, refData, quote, mode, change, connected }: {
  symbol: string; refData: InstrumentRef | null; quote: Quote | null;
  mode?: string; change?: number | null; connected: boolean;
}) {
  const avail = !!quote && (quote.status === "DATA_AVAILABLE" || quote.status === "DELAYED");
  const isLive = (mode || "").toUpperCase() === "LIVE";
  const quality = !connected ? "NO DATA" : !quote ? "NO DATA"
    : quote.status === "DATA_AVAILABLE" && quote.realtime ? "GOOD"
    : quote.status === "DELAYED" ? "DELAYED" : humanStatus(quote.status);
  return (
    <div className="thead">
      <div className="thead-id">
        <div className="sym">{symbol}</div>
        <div className="co">{refData?.company || symbol} · {refData?.exchange || quote?.region || NO_DATA}{refData?.assetClass ? ` · ${refData.assetClass}` : ""}</div>
      </div>
      <div className="thead-px">
        <div className={`price ${change != null ? (change >= 0 ? "up" : "down") : ""}`}>{price(quote?.last ?? null)}</div>
        <div className={`chg ${change != null ? (change >= 0 ? "up" : "down") : "neut"}`}>
          {change == null ? NO_DATA : `${change >= 0 ? "▲" : "▼"} ${pct(change)}`}
        </div>
      </div>
      <div className="thead-meta">
        <M label="Market">{!connected ? NO_DATA : <><Dot tone={avail ? "g" : "grey"} />{avail ? "OPEN" : "CLOSED"}</>}</M>
        <M label="Source"><Dot tone={quote?.source ? "t" : "grey"} />{quote?.source || NO_DATA}</M>
        <M label="Feed"><Dot tone={quote?.realtime ? "t" : "grey"} />{quote ? (quote.realtime ? "REALTIME" : "DELAYED") : NO_DATA}</M>
        <M label="Latency">{isPresent(quote?.latency) ? Math.round(quote!.latency as number) + "ms" : NO_DATA}</M>
        <M label="Last Update">{quote?.timestamp ? hhmmss(quote.timestamp) : NO_DATA}</M>
        <M label="Data Quality"><Tag kind={quality === "GOOD" ? "ok" : quality === "NO DATA" ? "muted" : "warnt"}>{quality}</Tag></M>
        <M label="Mode"><Tag kind={isLive ? "sell" : "muted"}>{isLive ? "LIVE" : "PAPER"}</Tag></M>
      </div>
    </div>
  );
}
