// Market Data Quality (pure): shows ONLY existing quote fields — source, status, latency, timestamp,
// exchange. Missing fields render NO DATA. No value is calculated or fabricated.
import React from "react";
import { NO_DATA, isPresent, hhmmss } from "@/lib/format";
import { humanStatus } from "@/lib/errors";
import type { Quote } from "@/lib/market";
import type { InstrumentRef } from "@/lib/instruments";

export function DataQuality({ quote, refData }: { quote: Quote | null; refData: InstrumentRef | null }) {
  const cells: [string, React.ReactNode][] = [
    ["Source", quote?.source ?? NO_DATA],
    ["Status", quote ? humanStatus(quote.status) : NO_DATA],
    ["Latency", isPresent(quote?.latency) ? Math.round(quote!.latency as number) + "ms" : NO_DATA],
    ["Timestamp", quote?.timestamp ? hhmmss(quote.timestamp) : NO_DATA],
    ["Exchange", refData?.exchange ?? quote?.region ?? NO_DATA],
  ];
  return (
    <div className="card dq">
      <div className="label" style={{ marginBottom: 10 }}>Market Data Quality</div>
      <div className="dq-grid">
        {cells.map(([k, v]) => <div className="dq-cell" key={k}><div className="label">{k}</div><div className="v num">{v}</div></div>)}
      </div>
    </div>
  );
}
