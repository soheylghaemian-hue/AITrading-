"use client";
// Options tab (§ Phase G2.3). Options intelligence score, put/call ratio, implied volatility, volume,
// open interest, unusual-activity flag, sentiment and deterministic signals/risks. Loading → spinner;
// error or no coverage → NO DATA. Intelligence signal only — never a trade signal, never fabricated.
import React, { useEffect, useState } from "react";
import { fetchOptions } from "@/lib/api";
import { NO_DATA, isPresent } from "@/lib/format";
import { compact, hasOptions, ivPct, premium, scoreTier, sentimentTone, type OptionsData } from "@/lib/options";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div>;
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="fmetric"><div className="label">{label}</div><div className="v num">{value}</div></div>;
}

export function OptionsList({ data, loading, error, symbol }: {
  data: OptionsData | null; loading: boolean; error: string | null; symbol: string;
}) {
  if (loading) return <Box title="LOADING" note="Fetching options intelligence…" />;
  if (error) return <Box title={NO_DATA} note="Options intelligence unavailable — nothing is shown, nothing invented." />;
  if (!hasOptions(data))
    return <Box title={NO_DATA} note="No options coverage for this symbol yet. Positioning appears once an options provider is connected — never fabricated." />;

  const d = data as OptionsData;
  const detected = d.unusual_activity === "Detected";

  return (
    <div className="fund">
      <div className="fhead">
        <div>
          <div className="label">{symbol} Options Intelligence</div>
          <div className="fsub">Positioning · volatility · unusual activity</div>
        </div>
        <div className="fscore">
          <div className="label" style={{ textAlign: "right" }}>Options Score</div>
          <div className={`fq ${scoreTier(d.options_score)}`}>{d.options_score == null ? NO_DATA : d.options_score}<small>{d.options_score == null ? "" : " / 100"}</small></div>
        </div>
      </div>

      <div className="fgrid">
        <Metric label="IV" value={ivPct(d.implied_volatility)} />
        <Metric label="Put / Call" value={isPresent(d.call_put_ratio) ? (d.call_put_ratio as number).toFixed(2) : NO_DATA} />
        <Metric label="Volume" value={compact(d.volume)} />
        <Metric label="Open Interest" value={compact(d.open_interest)} />
        <Metric label="Premium" value={premium(d.premium_volume)} />
        <Metric label="Sentiment" value={<span className={`fval ${sentimentTone(d.sentiment)}`}>{d.sentiment || NO_DATA}</span>} />
      </div>

      <div className="fanalyst">
        <div className="label">Unusual Activity</div>
        <div className="fameta">
          <span className={`fval ${detected ? "neg" : "pos"}`} style={{ marginTop: 6 }}>{d.unusual_activity || NO_DATA}</span>
          <span>Large Trades <b>{isPresent(d.large_trade_count) ? String(d.large_trade_count) : NO_DATA}</b></span>
          <span>Bias <b>{d.sentiment || NO_DATA}</b></span>
        </div>
      </div>

      {(d.signals.length || d.risks.length) ? (
        <div className="fsr">
          <div className="fcol">
            <div className="label">Signals</div>
            {d.signals.length ? d.signals.map((s) => <div className="fitem pos" key={s}>✓ {s}</div>)
              : <div className="fitem neu">{NO_DATA}</div>}
          </div>
          <div className="fcol">
            <div className="label">Risks</div>
            {d.risks.length ? d.risks.map((s) => <div className="fitem neg" key={s}>⚠ {s}</div>)
              : <div className="fitem neu">{NO_DATA}</div>}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function OptionsFeed({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: OptionsData | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchOptions(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <OptionsList data={state.data} loading={state.loading} error={state.error} symbol={symbol} />;
}
