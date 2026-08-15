// Professional multi-panel chart — PURE (no hooks); renders via renderToStaticMarkup in tests and on
// the server. Inline SVG, no dependencies. All indicators are COMPUTED from real closes (never invented).
//
// Layered architecture (future-ready):
//   • price layer      — candlesticks + volume            (REAL bars; never fabricated)
//   • indicator layer  — EMA20/50/200, VWAP, RSI-14, MACD (computed from real closes)
//   • AI signal layer  — entry / stop / target + marker   (ONLY when the decision fields exist)
//   • event layer      — reserved (news / earnings / signals) — NOT implemented yet
//
// Fewer than two real bars → renders nothing; the caller shows "HISTORICAL DATA NOT CONNECTED".
import React from "react";
import { isValidBar, type OhlcBar } from "@/lib/ohlc";
import { ema, rsi, vwap, macd, isNum } from "@/lib/indicators";

export interface AiOverlay {
  action?: string | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
}

/** Chart geometry, shared with the interactive crosshair layer (MarketChart). */
export const CHART_GEO = { W: 1040, PL: 8, PR: 54 };

export function CandleChart({ bars, ai }: { bars: OhlcBar[]; interval?: string; ai?: AiOverlay | null }) {
  const data = (bars || []).filter(isValidBar);       // real bars only — never fabricate/patch
  if (data.length < 2) return null;

  const { W, PL, PR } = CHART_GEO, PW = W - PL - PR;
  const P = { t: 10, h: 280 }, V = { t: 304, h: 40 }, R = { t: 356, h: 64 }, M = { t: 436, h: 96 };
  const closes = data.map((b) => b.close);
  const e20 = ema(20, closes), e50 = ema(50, closes), e200 = ema(200, closes);
  const vw = vwap(data), r14 = rsi(14, closes), mac = macd(closes);

  const levels = [ai?.entry, ai?.stop, ai?.target].filter(isNum) as number[];
  const pMin = Math.min(...data.map((b) => b.low), ...levels) - 1.2;
  const pMax = Math.max(...data.map((b) => b.high), ...levels) + 1.2;
  const vMax = Math.max(...data.map((b) => (isNum(b.volume) ? (b.volume as number) : 0)), 1);
  const mMax = Math.max(...mac.hist.map(Math.abs), ...mac.line.map(Math.abs), 1e-6);
  const slot = PW / data.length, bw = Math.max(2.5, slot * 0.62);
  const x = (i: number) => PL + i * slot + slot / 2;
  const yP = (v: number) => P.t + ((pMax - v) / (pMax - pMin)) * P.h;
  const yR = (v: number) => R.t + ((100 - v) / 100) * R.h;
  const yM = (v: number) => M.t + M.h / 2 - (v / mMax) * (M.h / 2 - 4);
  const line = (vals: number[]) => vals.map((v, i) => `${x(i).toFixed(1)},${yP(v).toFixed(1)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${W} 560`} preserveAspectRatio="xMidYMid meet" role="img"
      aria-label={`Candlestick chart with EMA20/50/200, VWAP, RSI-14 and MACD (${data.length} bars)`}
      style={{ width: "100%", height: "auto", display: "block", minWidth: 680 }}>

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
      {[V.t - 6, R.t - 6, M.t - 6].map((y, i) => <line key={i} x1={PL} y1={y} x2={PL + PW} y2={y} stroke="var(--line-soft)" strokeWidth="1" />)}
      {["VOL", "RSI 14", "MACD 12/26/9"].map((t, i) => (
        <text key={t} x={PL + 3} y={[V.t + 12, R.t + 13, M.t + 13][i]} fill="var(--faint)" fontFamily="var(--mono)" fontSize="10" fontWeight="600">{t}</text>
      ))}

      {/* ---------------- price layer: volume + candlesticks ---------------- */}
      <g data-layer="price">
        {data.map((b, i) => {
          const up = b.close >= b.open, h = ((isNum(b.volume) ? (b.volume as number) : 0) / vMax) * V.h;
          return <rect key={"v" + i} x={x(i) - bw / 2} y={V.t + V.h - h} width={bw} height={h} rx="1" fill={up ? "var(--pos)" : "var(--neg)"} opacity="0.35" />;
        })}
        {data.map((b, i) => {
          const up = b.close >= b.open, col = up ? "var(--pos)" : "var(--neg)";
          const yo = yP(b.open), yc = yP(b.close);
          return (
            <g key={"c" + i}>
              <line x1={x(i)} y1={yP(b.high)} x2={x(i)} y2={yP(b.low)} stroke={col} strokeWidth="1" />
              <rect x={x(i) - bw / 2} y={Math.min(yo, yc)} width={bw} height={Math.max(1, Math.abs(yc - yo))} rx="1" fill={col} />
            </g>
          );
        })}
      </g>

      {/* ---------------- indicator layer: EMA20/50/200, VWAP, RSI-14, MACD ---------------- */}
      <g data-layer="indicator">
        <polyline points={line(e200)} fill="none" stroke="var(--ema200)" strokeWidth="1.4" opacity="0.9" />
        <polyline points={line(e50)} fill="none" stroke="var(--ema50)" strokeWidth="1.5" opacity="0.95" />
        <polyline points={line(e20)} fill="none" stroke="var(--accent)" strokeWidth="1.5" opacity="0.95" />
        <polyline points={line(vw)} fill="none" stroke="var(--vwap)" strokeWidth="1.4" strokeDasharray="4 3" opacity="0.9" />
        {/* RSI panel */}
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
        {/* MACD panel */}
        <line x1={PL} y1={M.t + M.h / 2} x2={PL + PW} y2={M.t + M.h / 2} stroke="var(--line-soft)" strokeWidth="1" />
        {mac.hist.map((v, i) => {
          const y0 = M.t + M.h / 2, y1 = yM(v);
          return <rect key={"m" + i} x={x(i) - bw / 2} y={Math.min(y0, y1)} width={bw} height={Math.max(1, Math.abs(y1 - y0))} rx="1" fill={v >= 0 ? "var(--pos)" : "var(--neg)"} opacity="0.5" />;
        })}
        <polyline points={mac.line.map((v, i) => `${x(i).toFixed(1)},${yM(v).toFixed(1)}`).join(" ")} fill="none" stroke="var(--accent)" strokeWidth="1.3" />
        <polyline points={mac.signal.map((v, i) => `${x(i).toFixed(1)},${yM(v).toFixed(1)}`).join(" ")} fill="none" stroke="var(--ema50)" strokeWidth="1.3" />
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

      {/* ---------------- event layer: reserved (news / earnings / signals) — not implemented ---------------- */}
      <g data-layer="event" />
    </svg>
  );
}
