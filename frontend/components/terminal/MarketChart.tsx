"use client";
// Interactive chart shell: timeframe tabs + indicator legend + zoom/pan + crosshair, wrapping the pure
// CandleChart. Presentational — it renders the bars/loading/error handed to it by MarketTerminal (which
// fetches OHLC from the Control API through the server proxy). Candles are never fabricated: loading →
// "Loading market history…", error → "Market history unavailable", empty → "HISTORICAL DATA NOT CONNECTED".
import React, { useEffect, useRef, useState } from "react";
import { CandleChart, CHART_GEO, type AiOverlay } from "@/components/CandleChart";
import { INTERVALS, isValidBar, type OhlcBar } from "@/lib/ohlc";

const LEGEND: [string, string][] = [
  ["Up", "var(--pos)"], ["Down", "var(--neg)"], ["EMA20", "var(--accent)"], ["EMA50", "var(--ema50)"],
  ["EMA200", "var(--ema200)"], ["VWAP", "var(--vwap)"], ["RSI", "var(--rsi)"],
];

function StateBox({ kind }: { kind: "loading" | "error" | "empty" }) {
  const map = {
    loading: ["Loading market history…", "Fetching candles from the read-only backend."],
    error: ["Market history unavailable", "The OHLC endpoint could not be reached — no candles are shown, nothing is fabricated."],
    empty: ["HISTORICAL DATA NOT CONNECTED", "No OHLC bars for this symbol/interval yet. Candles, EMA, VWAP, RSI and MACD appear once real bars exist — never fabricated."],
  } as const;
  const [title, sub] = map[kind];
  return (
    <div className="chartempty">
      <svg className="ic" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><path d="M3 3v18h18" /><rect x="7" y="10" width="2.4" height="6" /><rect x="12" y="6" width="2.4" height="10" /><rect x="17" y="12" width="2.4" height="4" /></svg>
      <div className="t" style={kind === "empty" ? { fontFamily: "var(--mono)", letterSpacing: 1 } : undefined}>{title}</div>
      <p>{sub}</p>
    </div>
  );
}

export function MarketChart({ bars, loading, error, interval, onInterval, ai }: {
  bars: OhlcBar[] | null;
  loading: boolean;
  error: string | null;
  interval: string;
  onInterval: (iv: string) => void;
  ai?: AiOverlay | null;
}) {
  const clean = (bars || []).filter(isValidBar);
  const boxRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(90);
  const [off, setOff] = useState(0);
  const [hover, setHover] = useState<{ px: number; i: number } | null>(null);
  const drag = useRef<{ x: number; off: number } | null>(null);

  const count = Math.min(clean.length, Math.max(20, zoom));
  const end = Math.max(count, clean.length - off);
  const start = Math.max(0, end - count);
  const visible = clean.slice(start, end);

  useEffect(() => { setZoom(90); setOff(0); setHover(null); }, [interval, clean.length]); // reset on new series
  useEffect(() => {
    const box = boxRef.current; if (!box || clean.length === 0) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setZoom((z) => Math.min(clean.length, Math.max(20, Math.round(z * (e.deltaY > 0 ? 1.12 : 0.89)))));
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, [clean.length]);

  const hasChart = !loading && !error && clean.length >= 2;
  const geo = CHART_GEO, PW = geo.W - geo.PL - geo.PR;
  function onMove(e: React.MouseEvent) {
    const box = boxRef.current; if (!box) return;
    const rect = box.getBoundingClientRect();
    if (drag.current) {
      const dxBars = Math.round(((e.clientX - drag.current.x) / rect.width) * count);
      setOff(Math.max(0, Math.min(clean.length - count, drag.current.off + dxBars)));
      return;
    }
    const vbX = ((e.clientX - rect.left) / rect.width) * geo.W;
    const i = Math.round((vbX - geo.PL) / (PW / Math.max(1, visible.length)) - 0.5);
    if (i >= 0 && i < visible.length) setHover({ px: e.clientX - rect.left, i });
    else setHover(null);
  }
  const b = hover ? visible[hover.i] : null;

  return (
    <>
      <div className="ctb">
        <div className="strip">
          {INTERVALS.map((iv) => (
            <button key={iv} className={`pill${interval === iv ? " ivon" : ""}`} onClick={() => onInterval(iv)}
              style={{ cursor: "pointer", font: "inherit" }} aria-pressed={interval === iv}>{iv}</button>
          ))}
        </div>
        <div className="chart-legend">{LEGEND.map(([n, c]) => <span key={n}><span className={n === "Up" || n === "Down" ? "swb" : "sw"} style={{ background: c }} />{n}</span>)}</div>
      </div>
      <div className="chartbox" ref={boxRef}
        onMouseMove={hasChart ? onMove : undefined}
        onMouseDown={hasChart ? (e) => { drag.current = { x: e.clientX, off }; } : undefined}
        onMouseUp={() => { drag.current = null; }}
        onMouseLeave={() => { drag.current = null; setHover(null); }}>
        {loading ? <StateBox kind="loading" />
          : error ? <StateBox kind="error" />
          : hasChart ? (
            <>
              <CandleChart bars={visible} interval={interval} ai={ai} />
              {hover ? <div className="cross-v" style={{ left: hover.px }} /> : null}
              {b ? (
                <div className="chart-tip num">
                  O <b>{b.open.toFixed(2)}</b> H <b>{b.high.toFixed(2)}</b> L <b>{b.low.toFixed(2)}</b>{" "}
                  C <b className={b.close >= b.open ? "up" : "down"}>{b.close.toFixed(2)}</b>
                  {b.volume != null ? <> · Vol <b>{(b.volume / 1e6).toFixed(2)}M</b></> : null}
                </div>
              ) : null}
              <div className="zoomhint">scroll to zoom · drag to pan · {visible.length}/{clean.length} bars</div>
            </>
          ) : <StateBox kind="empty" />}
      </div>
    </>
  );
}
