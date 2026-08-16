"use client";
// AI History tab (§ Phase G3.1) — past AI Views for the symbol with their measured outcomes (forward
// return after each horizon). Immutable history; loading → spinner; no history / no outcomes → NO DATA.
// Read-only; never rewrites or fabricates a past prediction.
import React, { useEffect, useState } from "react";
import { fetchAiHistory } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { directionTone, hasHistory, shortDate, type AiHistory, type AiHistoryItem } from "@/lib/performance";
import { govTone } from "@/lib/governance";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div>;
}

function outcomeText(item: AiHistoryItem): { text: string; tone: string } {
  const o = item.outcomes.find((x) => x.time_horizon === 5) || item.outcomes[item.outcomes.length - 1];
  if (!o || o.return_percentage == null) return { text: "pending", tone: "neu" };
  const r = o.return_percentage;
  const mark = o.direction_correct == null ? "" : o.direction_correct ? " ✓" : " ✗";
  return { text: `${r >= 0 ? "+" : ""}${r.toFixed(1)}% after ${o.time_horizon}d${mark}`,
           tone: o.direction_correct == null ? "neu" : o.direction_correct ? "pos" : "neg" };
}

export function AiHistoryList({ data, loading, error, symbol }: {
  data: AiHistory | null; loading: boolean; error: string | null; symbol: string;
}) {
  if (loading) return <Box title="LOADING" note="Loading AI history…" />;
  if (error) return <Box title={NO_DATA} note="AI history unavailable — nothing is shown, nothing invented." />;
  if (!hasHistory(data))
    return <Box title={NO_DATA} note="No past AI views for this symbol yet. Predictions and their measured outcomes appear here as they mature — history is never rewritten." />;

  const d = data as AiHistory;
  return (
    <div className="newsfeed">
      {d.assessments.map((item) => {
        const oc = outcomeText(item);
        return (
          <div className="aihrow" key={item.id}>
            <span className="aihdate">{symbol} · {shortDate(item.timestamp)}</span>
            <span className="aihmeta">
              <span className={`consb ${directionTone(item.direction)}`}>{item.direction || NO_DATA}</span>
              {item.governance?.status
                ? <span className={`aihgov ${govTone(item.governance.status)}`}>{item.governance.status}</span> : null}
            </span>
            <span className="aihscore num">Score {item.score == null ? NO_DATA : item.score}</span>
            <span className={`aihout num ${oc.tone}`}>{oc.text}</span>
          </div>
        );
      })}
    </div>
  );
}

export function AiHistoryFeed({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: AiHistory | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchAiHistory(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <AiHistoryList data={state.data} loading={state.loading} error={state.error} symbol={symbol} />;
}
