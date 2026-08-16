"use client";
// News tab feed (§ Phase G2.1). Fetches real headlines for the symbol from the Control API through the
// same-origin proxy and renders title / source / time / sentiment / impact. Loading → spinner text;
// error or empty → NO DATA. Only valid items render — no fabricated headlines.
import React, { useEffect, useState } from "react";
import { fetchNews } from "@/lib/api";
import { NO_DATA } from "@/lib/format";
import { impactTone, isValidNewsItem, newsTime, sentimentTone, type NewsItem } from "@/lib/news";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div>;
}

export function NewsList({ items, loading, error }: { items: NewsItem[] | null; loading: boolean; error: string | null }) {
  if (loading) return <Box title="LOADING" note="Fetching market news…" />;
  if (error) return <Box title={NO_DATA} note="News feed unavailable — nothing is shown, nothing invented." />;
  const clean = (items || []).filter(isValidNewsItem);
  if (clean.length === 0)
    return <Box title={NO_DATA} note="No news for this symbol yet. Headlines appear once the news-intelligence service collects them — never invented." />;
  return (
    <div className="newsfeed">
      {clean.map((n) => {
        const row = (
          <>
            <div className="newshead">
              <span className={`sentb ${sentimentTone(n.sentiment)}`}>{(n.sentiment || "—").toUpperCase()}</span>
              <span className={`impb ${impactTone(n.impact)}`}>{n.impact || "—"}</span>
              <span className="newstime">{newsTime(n.published_at)}</span>
            </div>
            <div className="newstitle">{n.title}</div>
            <div className="newsmeta">{n.source || "—"}</div>
          </>
        );
        return n.url
          ? <a className="newsrow" key={n.id} href={n.url} target="_blank" rel="noopener noreferrer">{row}</a>
          : <div className="newsrow" key={n.id}>{row}</div>;
      })}
    </div>
  );
}

export function NewsFeed({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ items: NewsItem[] | null; loading: boolean; error: string | null }>({
    items: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ items: s.items, loading: true, error: null }));
    fetchNews(symbol, 30, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ items: r.items, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ items: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <NewsList items={state.items} loading={state.loading} error={state.error} />;
}
