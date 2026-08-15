"use client";
// Interactive chart shell: timeframe tabs + indicator legend + zoom/pan + crosshair tooltip, wrapping
// the pure CandleChart. When there is no OHLC series it shows "HISTORICAL DATA NOT CONNECTED".
import React, { useEffect, useRef, useState } from "react";
import { CandleChart, CHART_GEO, type AiOverlay } from "@/components/CandleChart";
import { INTERVALS, type OhlcSeries } from "@/lib/ohlc";

function ChartUnavailable() {
  return (
    <div className="chartempty">
      <svg className="ic" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><path d="M3 3v18h18" /><rect x="7" y="10" width="2.4" height="6" /><rect x="12" y="6" width="2.4" height="10" /><rect x="17" y="12" width="2.4" height="4" /></svg>
      <div className="t" style={{ fontFamily: "var(--mono)", letterSpacing: 1 }}>HISTORICAL DATA NOT CONNECTED</div>
      <p>No OHLC bar feed is connected. Candlesticks, EMA, VWAP, RSI and MACD appear here once the backend serves an OHLC series — candles are never fabricated.</p>
    </div>
  );
}

const LEGEND: [string, string][] = [
  ["Up", "var(--pos)"], ["Down", "var(--neg)"], ["EMA20", "var(--accent)"], ["EMA50", "var(--ema50)"],
  ["EMA200", "var(--ema200)"], ["VWAP", "var(--vwap)"], ["RSI", "var(--rsi)"],
];

export function MarketChart({ series, ai }: { series: OhlcSeries | null; ai?: AiOverlay | null }) {
  const bars = series?.bars ?? [];
  const boxRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(90);        // visible bar count
  const [off, setOff] = useState(0);           // pan offset from the most recent bar
  const [hover, setHover] = useState<{ px: number; i: number } | null>(null);
  const drag = useRef<{ x: number; off: number } | null>(null);

  const count = Math.min(bars.length, Math.max(20, zoom));
  const end = Math.max(count, bars.length - off);
  const start = Math.max(0, end - count);
  const visible = bars.slice(start, end);

  // non-passive wheel listener so we can preventDefault the page scroll while zooming
  useEffect(() => {
    const box = boxRef.current; if (!box || bars.length === 0) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setZoom((z) => Math.min(bars.length, Math.max(20, Math.round(z * (e.deltaY > 0 ? 1.12 : 0.89)))));
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, [bars.length]);

  if (!series || bars.length < 2) return <><Toolbar interval={series?.interval} /><div className="chartbox">{<ChartUnavailable />}</div></>;

  const geo = CHART_GEO, PW = geo.W - geo.PL - geo.PR;
  function onMove(e: React.MouseEvent) {
    const box = boxRef.current; if (!box) return;
    const rect = box.getBoundingClientRect();
    if (drag.current) {
      const dxBars = Math.round(((e.clientX - drag.current.x) / rect.width) * count);
      setOff(Math.max(0, Math.min(bars.length - count, drag.current.off + dxBars)));
      return;
    }
    const vbX = ((e.clientX - rect.left) / rect.width) * geo.W;
    const i = Math.round((vbX - geo.PL) / (PW / visible.length) - 0.5);
    if (i >= 0 && i < visible.length) setHover({ px: e.clientX - rect.left, i });
    else setHover(null);
  }
  const b = hover ? visible[hover.i] : null;

  return (
    <>
      <Toolbar interval={series.interval} />
      <div className="chartbox" ref={boxRef}
        onMouseMove={onMove}
        onMouseDown={(e) => { drag.current = { x: e.clientX, off }; }}
        onMouseUp={() => { drag.current = null; }}
        onMouseLeave={() => { drag.current = null; setHover(null); }}>
        <CandleChart bars={visible} interval={series.interval} ai={ai} />
        {hover ? <div className="cross-v" style={{ left: hover.px }} /> : null}
        {b ? (
          <div className="chart-tip num">
            O <b>{b.open.toFixed(2)}</b> H <b>{b.high.toFixed(2)}</b> L <b>{b.low.toFixed(2)}</b>{" "}
            C <b className={b.close >= b.open ? "up" : "down"}>{b.close.toFixed(2)}</b>
            {b.volume != null ? <> · Vol <b>{(b.volume / 1e6).toFixed(2)}M</b></> : null}
          </div>
        ) : null}
        <div className="zoomhint">scroll to zoom · drag to pan · {visible.length}/{bars.length} bars</div>
      </div>
    </>
  );
}

function Toolbar({ interval }: { interval?: string }) {
  return (
    <div className="ctb">
      <div className="strip">{INTERVALS.map((iv) => <span className={`pill${interval === iv ? " ivon" : ""}`} key={iv}>{iv}</span>)}</div>
      <div className="chart-legend">{LEGEND.map(([n, c]) => <span key={n}><span className={n === "Up" || n === "Down" ? "swb" : "sw"} style={{ background: c }} />{n}</span>)}</div>
    </div>
  );
}
