// Options intelligence types (§ Phase G2.3). Mirrors the Control API's /market/{symbol}/options.
// Intelligence signal only — never a trade signal. IV / volume / OI / flow / unusual activity are never
// fabricated; missing → NO DATA.
import { NO_DATA } from "./format";

export interface OptionsData {
  symbol: string;
  options_score: number | null;
  call_put_ratio: number | null;
  implied_volatility: number | null;
  volume: number | null;
  call_volume: number | null;
  put_volume: number | null;
  open_interest: number | null;
  premium_volume: number | null;
  unusual_activity: string | null;      // "Detected" / "Normal"
  unusual_activity_score: number | null;
  large_trade_count: number | null;
  sentiment: string | null;             // Bullish / Bearish / Neutral
  signals: string[];
  risks: string[];
}

/** True only when a real options score exists (else the tab shows NO DATA). */
export function hasOptions(o: OptionsData | null | undefined): boolean {
  return !!o && o.options_score != null;
}

export function scoreTier(q: number | null | undefined): "hi" | "med" | "lo" {
  if (q == null) return "lo";
  return q >= 70 ? "hi" : q >= 45 ? "med" : "lo";
}

export function sentimentTone(s: string | null | undefined): "pos" | "neg" | "neu" {
  return s === "Bullish" ? "pos" : s === "Bearish" ? "neg" : "neu";
}

/** A fraction (0.42) → "42.0%". Null → NO DATA. */
export function ivPct(x: number | null | undefined): string {
  return x == null ? NO_DATA : `${(x * 100).toFixed(1)}%`;
}

/** Compact volume, e.g. 48000 → "48.0K". Null → NO DATA. */
export function compact(x: number | null | undefined): string {
  if (x == null) return NO_DATA;
  const a = Math.abs(x);
  if (a >= 1e9) return `${(x / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(x / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(x / 1e3).toFixed(1)}K`;
  return `${x}`;
}

/** Premium in dollars → "$19.2M". Null → NO DATA. */
export function premium(x: number | null | undefined): string {
  if (x == null) return NO_DATA;
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(x / 1e3).toFixed(0)}K`;
  return `$${x.toFixed(0)}`;
}
