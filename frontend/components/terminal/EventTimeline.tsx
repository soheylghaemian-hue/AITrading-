// Event timeline (pure) — PREPARED for future sources: News · Earnings · AI Signals · Macro Events.
// It renders whatever real events it is given; with no events it shows NO DATA. Nothing is invented.
import React from "react";
import { NO_DATA, hhmmss } from "@/lib/format";
import { NoData, Tag } from "../ui";

export type EventKind = "signal" | "news" | "earnings" | "macro";
export interface TimelineEvent {
  ts: string;
  kind: EventKind;
  title: string;
  detail?: string | null;
  tone?: "buy" | "sell" | "muted" | "warnt" | "ok";
}

const KIND_LABEL: Record<EventKind, string> = { signal: "AI SIGNAL", news: "NEWS", earnings: "EARNINGS", macro: "MACRO" };

export function EventTimeline({ events }: { events?: TimelineEvent[] }) {
  const rows = (events || []).slice(0, 30);
  if (rows.length === 0) {
    return <NoData note="No events connected. AI signals, news, earnings and macro events appear here once their sources are wired — never invented." />;
  }
  return (
    <div className="timeline">
      {rows.map((e, i) => (
        <div className="tl-i" key={i}>
          <div className="tl-t">{hhmmss(e.ts)}</div>
          <div className="tl-rail"><span className={`tl-node ${e.tone === "sell" ? "sell" : e.tone === "buy" ? "buy" : "no_data"}`} />{i < rows.length - 1 ? <span className="tl-line" /> : null}</div>
          <div className="tl-body">
            <div className="tl-head"><Tag kind="muted">{KIND_LABEL[e.kind]}</Tag><b>{e.title}</b>{e.tone ? <Tag kind={e.tone}>{e.tone.toUpperCase()}</Tag> : null}</div>
            {e.detail ? <div className="tl-reason">{e.detail}</div> : null}
          </div>
        </div>
      ))}
    </div>
  );
}
