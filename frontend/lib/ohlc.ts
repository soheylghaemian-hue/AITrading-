// Optional, FUTURE OHLC contract for the Market Intelligence chart. These are additive frontend types:
// the read-model MAY one day carry a per-symbol OHLC map, but nothing here changes what the backend
// must send today (no API-contract change). When the field is absent the chart renders NO DATA —
// candles are NEVER fabricated.

export interface OhlcBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface OhlcSeries {
  interval: string;                 // one of INTERVALS
  bars: OhlcBar[];
}

export const INTERVALS = ["1m", "5m", "15m", "1h", "1D"] as const;
export type Interval = (typeof INTERVALS)[number];

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** A bar is usable only if O/H/L/C are all real numbers. Malformed/partial bars are dropped, never patched. */
export function isValidBar(b: any): b is OhlcBar {
  return !!b && isNum(b.open) && isNum(b.high) && isNum(b.low) && isNum(b.close);
}

/** Read the optional per-symbol OHLC series off a snapshot. Returns null when absent, malformed or empty
 *  (→ caller shows the honest "Historical chart unavailable" state). Never fabricates bars. */
export function ohlcForSymbol(snapshot: any, symbol: string): OhlcSeries | null {
  const ohlc = snapshot?.ohlc;
  if (!ohlc || !symbol) return null;
  const raw = ohlc[symbol] ?? ohlc[symbol.toUpperCase()] ?? ohlc[symbol.toLowerCase()];
  if (!raw || !Array.isArray(raw.bars)) return null;
  const bars = raw.bars.filter(isValidBar);
  if (bars.length < 2) return null;                 // need at least two real bars to draw anything
  return { interval: typeof raw.interval === "string" ? raw.interval : "1D", bars };
}
