// Normalize a single symbol's quote from the snapshot's global_market_data / market_data. Returns null
// when the symbol has no quote (→ NO DATA). Never fabricates prices.
export interface Quote {
  symbol: string;
  source: string | null;
  realtime: boolean;
  bid: number | null;
  ask: number | null;
  last: number | null;
  volume: number | null;
  latency: number | null;
  status: string;
  code: number | null;
  reason: string | null;
  region?: string;
  timestamp?: string | null;
}

export function symbolQuote(s: any, symbol: string): Quote | null {
  if (!s || !symbol) return null;
  const up = symbol.toUpperCase();
  const g = (s.global_market_data || []).find((r: any) => (r.symbol || "").toUpperCase() === up);
  if (g) return {
    symbol: g.symbol, source: g.source ?? null, realtime: !!g.realtime, bid: g.bid ?? null, ask: g.ask ?? null,
    last: g.last ?? null, volume: g.volume ?? null, latency: g.latency_ms ?? null, status: g.status,
    code: null, reason: g.error ?? null, region: g.region, timestamp: g.timestamp ?? null,
  };
  const m = (s.market_data || []).find((r: any) => (r.symbol || "").toUpperCase() === up);
  if (m) return {
    symbol: m.symbol, source: null, realtime: m.market_data_type === "REALTIME", bid: m.bid ?? null, ask: m.ask ?? null,
    last: m.last ?? null, volume: null, latency: null, status: m.status, code: m.error_code ?? null,
    reason: m.reason ?? null, timestamp: m.timestamp ?? null,
  };
  return null;
}
