// News intelligence types (§ Phase G2.1). Shape mirrors the Control API's /market/{symbol}/news items.
// Every item traces to a real headline; there is no fabricated news. Absent → the terminal shows NO DATA.

export type Sentiment = "positive" | "neutral" | "negative";
export type Impact = "LOW" | "MEDIUM" | "HIGH";

export interface NewsItem {
  id: string;
  symbol: string;
  title: string;
  source: string | null;
  url: string | null;
  published_at: string;
  summary: string | null;
  sentiment_score: number | null;
  sentiment: Sentiment | null;
  impact: Impact | null;
}

/** A news item is renderable only if it carries a real title + timestamp. Partial items are dropped. */
export function isValidNewsItem(n: any): n is NewsItem {
  return !!n && typeof n.title === "string" && n.title.trim().length > 0 && typeof n.published_at === "string";
}

export function sentimentTone(s: string | null | undefined): "pos" | "neg" | "neu" {
  return s === "positive" ? "pos" : s === "negative" ? "neg" : "neu";
}

export function impactTone(i: string | null | undefined): "hi" | "med" | "lo" {
  return i === "HIGH" ? "hi" : i === "MEDIUM" ? "med" : "lo";
}

/** Short absolute time for a headline (e.g. "Aug 16 · 14:30"), or "—" for a bad/absent timestamp. */
export function newsTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
