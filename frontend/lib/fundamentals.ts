// Fundamentals intelligence types (§ Phase G2.2). Mirrors the Control API's /market/{symbol}/fundamentals.
// Intelligence signal only — never a buy/sell decision. Every value traces to real persisted data or is
// NO DATA. Earnings/revenue/margins/valuation/analyst data are never fabricated.
import { NO_DATA } from "./format";

export interface FundamentalsData {
  symbol: string;
  company: { company_name: string | null; sector: string | null; industry: string | null; exchange: string | null; country: string | null } | null;
  quality_score: number | null;
  quality_breakdown: Record<string, number | null> | null;
  financials: {
    period: string | null; revenue: number | null; revenue_growth: number | null;
    gross_margin: number | null; operating_margin: number | null; net_margin: number | null;
    eps: number | null; eps_growth: number | null; free_cash_flow: number | null;
    debt: number | null; cash: number | null;
  } | null;
  valuation: { market_cap: number | null; pe_ratio: number | null; forward_pe: number | null; price_sales: number | null; enterprise_value: number | null } | null;
  analyst_estimates: { rating: string | null; target_price: number | null; analyst_count: number | null; upgrade_count: number | null; downgrade_count: number | null } | null;
  strengths: string[];
  risks: string[];
}

/** True only when real fundamentals coverage exists (else the tab shows NO DATA). */
export function hasFundamentals(f: FundamentalsData | null | undefined): boolean {
  return !!f && (!!f.company || !!f.financials || f.quality_score != null);
}

export function qualityTier(q: number | null | undefined): "hi" | "med" | "lo" {
  if (q == null) return "lo";
  return q >= 75 ? "hi" : q >= 50 ? "med" : "lo";
}

/** A fraction (0.35) → "35.0%". Null → NO DATA. */
export function pct(x: number | null | undefined): string {
  return x == null ? NO_DATA : `${(x * 100).toFixed(1)}%`;
}

/** Compact large money, e.g. 3.0e12 → "$3.00T". Null → NO DATA. */
export function big(x: number | null | undefined): string {
  if (x == null) return NO_DATA;
  const a = Math.abs(x);
  if (a >= 1e12) return `$${(x / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `$${(x / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(2)}M`;
  return `$${x.toFixed(0)}`;
}

/** Qualitative valuation label from P/E. Null → NO DATA. */
export function valuationLabel(pe: number | null | undefined): string {
  if (pe == null) return NO_DATA;
  return pe > 35 ? "High" : pe > 18 ? "Fair" : "Low";
}

export function valuationTone(pe: number | null | undefined): "neg" | "neu" | "pos" {
  if (pe == null) return "neu";
  return pe > 35 ? "neg" : pe > 18 ? "neu" : "pos";
}
