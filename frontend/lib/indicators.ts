// Technical indicators — all DERIVED from real bars. Never fabricate candles or values; these are
// deterministic transforms of the input series. Pure + unit-tested.
import type { OhlcBar } from "./ohlc";

export const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function ema(period: number, values: number[]): number[] {
  if (values.length === 0) return [];
  const k = 2 / (period + 1);
  let e = values[0];
  return values.map((c, i) => (e = i === 0 ? c : c * k + e * (1 - k)));
}

export function rsi(period: number, closes: number[]): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length <= period) return out;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const ch = closes[i] - closes[i - 1];
    avgGain += Math.max(ch, 0);
    avgLoss += Math.max(-ch, 0);
  }
  avgGain /= period; avgLoss /= period;
  out[period] = 100 - 100 / (1 + avgGain / (avgLoss || 1e-9));
  for (let i = period + 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(ch, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-ch, 0)) / period;
    out[i] = 100 - 100 / (1 + avgGain / (avgLoss || 1e-9));
  }
  return out;
}

/** Session VWAP: cumulative(typical price × volume) / cumulative(volume). Bars with no volume contribute 0. */
export function vwap(bars: Pick<OhlcBar, "high" | "low" | "close" | "volume">[]): number[] {
  let cpv = 0, cv = 0;
  return bars.map((b) => {
    const tp = (b.high + b.low + b.close) / 3;
    const v = isNum(b.volume) ? (b.volume as number) : 0;
    cpv += tp * v; cv += v;
    return cv ? cpv / cv : b.close;
  });
}

export function macd(closes: number[], fast = 12, slow = 26, sig = 9): { line: number[]; signal: number[]; hist: number[] } {
  const ef = ema(fast, closes), es = ema(slow, closes);
  const line = closes.map((_, i) => ef[i] - es[i]);
  const signal = ema(sig, line);
  const hist = line.map((v, i) => v - signal[i]);
  return { line, signal, hist };
}
