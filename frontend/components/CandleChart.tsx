// Professional TradingView-style chart — PURE (no hooks), so it renders via renderToStaticMarkup in
// tests and on the server. Inline SVG, no new dependencies.
//
// Layered architecture (future-ready; only the first three are implemented today):
//   • price layer      — candlesticks + volume            (from REAL bars; never fabricated)
//   • indicator layer  — EMA20, EMA50, RSI-14             (COMPUTED from real closes, not invented)
//   • AI signal layer  — entry / stop / target + marker   (ONLY when the decision fields exist)
//   • event layer      — reserved (crosshair / news / fills) — NOT implemented yet
//
// If there are fewer than two real bars the component renders nothing and the caller shows the honest
// "Historical chart unavailable" state.
import React from "react";
import { isValidBar, type OhlcBar } from "@/lib/ohlc";

export interface AiOverlay {
  action?: string | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function ema(period: number, closes: number[]): number[] {
  const k = 2 / (period + 1);
  let e = closes[0];
  return closes.map((c, i) => (e = i === 0 ? c : c * k + e * (1 - k)));
}

function rsi(period: number, closes: number[]): (number | null)[] {
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

export function CandleChart({ bars, ai }: { bars: OhlcBar[]; interval?: string; ai?: AiOverlay | null }) {
  const data = (bars || []).filter(isValidBar);       // real bars only — never fabricate/patch
  if (data.length < 2) return null;                    // caller renders "Historical chart unavailable"

  const W = 1040, PL = 8, PR = 54, PW = W - PL - PR;
  const P = { t: 12, h: 318 }, V = { t: 344, h: 64 }, R = { t: 430, h: 120 };
  const closes = data.map((b) => b.close);
  const e20 = ema(20, closes), e50 = ema(50, closes), r14 = rsi(14, closes);

  const levels = [ai?.entry, ai?.stop, ai?.target].filter(isNum) as number[];
  const pMin = Math.min(...data.map((b) => b.low), ...levels) - 1.2;
  const pMax = Math.max(...data.map((b) => b.high), ...levels) + 1.2;
  const vMax = Math.max(...data.map((b) => (isNum(b.volume) ? (b.volume as number) : 0)), 1);
  const slot = PW / data.length, bw = Math.max(2.5, slot * 0.62);
  const x = (i: number) => PL + i * slot + slot / 2;
  const yP = (v: number) => P.t + ((pMax - v) / (pMax - pMin)) * P.h;
  const yR = (v: number) => R.t + ((100 - v) / 100) * R.h;
  const line = (vals: number[]) => vals.map((v, i) => `${x(i).toFixed(1)},${yP(v).toFixed(1)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${W} 580`} preserveAspectRatio="xMidYMid meet" role="img"
      aria-label={`Candlestick chart with EMA20, EMA50, volume and RSI-14 (${data.length} bars)`}
      style={{ width: "100%", height: "auto", display: "block", minWidth: 640 }}>

      {/* price grid + right axis */}
      {[0, 1, 2, 3, 4].map((g) => {
        const val = pMax - ((pMax - pMin) * g) / 4, y = yP(val);
        return (
          <g key={g}>
            <line x1={PL} y1={y} x2={PL + PW} y2={y} stroke="var(--line-soft)" strokeWidth="1" />
            <text x={PL + PW + 6} y={y + 3} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10">{val.toFixed(2)}</text>
          </g>
        );
      })}
      <line x1={PL} y1={V.t - 8} x2={PL + PW} y2={V.t - 8} stroke="var(--line-soft)" strokeWidth="1" />
      <line x1={PL} y1={R.t - 8} x2={PL + PW} y2={R.t - 8} stroke="var(--line-soft)" strokeWidth="1" />

      {/* ---------------- price layer: volume + candlesticks ---------------- */}
      <g data-layer="price">
        {data.map((b, i) => {
          const up = b.close >= b.open, h = ((isNum(b.volume) ? (b.volume as number) : 0) / vMax) * V.h;
          return <rect key={"v" + i} x={x(i) - bw / 2} y={V.t + V.h - h} width={bw} height={h} rx="1" fill={up ? "var(--pos)" : "var(--neg)"} opacity="0.35" />;
        })}
        {data.map((b, i) => {
          const up = b.close >= b.open, col = up ? "var(--pos)" : "var(--neg)";
          const yo = yP(b.open), yc = yP(b.close), top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
          return (
            <g key={"c" + i}>
              <line x1={x(i)} y1={yP(b.high)} x2={x(i)} y2={yP(b.low)} stroke={col} strokeWidth="1" />
              <rect x={x(i) - bw / 2} y={top} width={bw} height={hgt} rx="1" fill={col} />
            </g>
          );
        })}
      </g>

      {/* ---------------- indicator layer: EMA20, EMA50, RSI-14 ---------------- */}
      <g data-layer="indicator">
        <polyline points={line(e20)} fill="none" stroke="var(--accent)" strokeWidth="1.6" opacity="0.95" />
        <polyline points={line(e50)} fill="none" stroke="var(--ema50)" strokeWidth="1.6" opacity="0.95" />
        {[30, 50, 70].map((l) => {
          const y = yR(l);
          return (
            <g key={l}>
              <line x1={PL} y1={y} x2={PL + PW} y2={y} stroke="var(--line-soft)" strokeWidth="1" strokeDasharray={l === 50 ? "0" : "4 4"} />
              <text x={PL + PW + 6} y={y + 3} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10">{l}</text>
            </g>
          );
        })}
        <polyline points={r14.map((v, i) => (v == null ? "" : `${x(i).toFixed(1)},${yR(v).toFixed(1)}`)).filter(Boolean).join(" ")}
          fill="none" stroke="var(--rsi)" strokeWidth="1.5" />
        <text x={PL + 3} y={R.t + 13} fill="var(--rsi)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">RSI 14</text>
      </g>

      {/* ---------------- AI signal layer: only when the decision fields exist ---------------- */}
      <g data-layer="ai">
        {isNum(ai?.entry) && (
          <g>
            <line x1={PL} y1={yP(ai!.entry as number)} x2={PL + PW} y2={yP(ai!.entry as number)} stroke="var(--accent)" strokeWidth="1.3" opacity="0.85" />
            <text x={PL + 3} y={yP(ai!.entry as number) - 4} fill="var(--accent)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">ENTRY {(ai!.entry as number).toFixed(2)}</text>
            <path d={`M${x(data.length - 1) - 6} ${yP(ai!.entry as number) + 14} L${x(data.length - 1) + 6} ${yP(ai!.entry as number) + 14} L${x(data.length - 1)} ${yP(ai!.entry as number) + 6} Z`}
              fill={(ai?.action || "").toUpperCase() === "SELL" ? "var(--neg)" : "var(--pos)"} />
          </g>
        )}
        {isNum(ai?.stop) && (
          <g>
            <line x1={PL} y1={yP(ai!.stop as number)} x2={PL + PW} y2={yP(ai!.stop as number)} stroke="var(--neg)" strokeWidth="1.3" strokeDasharray="5 4" opacity="0.85" />
            <text x={PL + 3} y={yP(ai!.stop as number) - 4} fill="var(--neg)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">STOP {(ai!.stop as number).toFixed(2)}</text>
          </g>
        )}
        {isNum(ai?.target) && (
          <g>
            <line x1={PL} y1={yP(ai!.target as number)} x2={PL + PW} y2={yP(ai!.target as number)} stroke="var(--pos)" strokeWidth="1.3" strokeDasharray="5 4" opacity="0.85" />
            <text x={PL + 3} y={yP(ai!.target as number) - 4} fill="var(--pos)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">TARGET {(ai!.target as number).toFixed(2)}</text>
          </g>
        )}
      </g>

      {/* ---------------- event layer: reserved for future (crosshair / news / fills) — not implemented ---------------- */}
      <g data-layer="event" />
    </svg>
  );
}
