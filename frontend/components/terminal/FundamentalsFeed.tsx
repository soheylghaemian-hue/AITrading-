"use client";
// Fundamentals tab (§ Phase G2.2). Company quality score, growth, profitability, valuation, analyst
// consensus, and deterministic strengths/risks. Loading → spinner text; error or no coverage → NO DATA.
// Intelligence signal only — never a buy/sell decision, never a fabricated financial value.
import React, { useEffect, useState } from "react";
import { fetchFundamentals } from "@/lib/api";
import { NO_DATA, isPresent } from "@/lib/format";
import { big, hasFundamentals, pct, qualityTier, valuationLabel, valuationTone, type FundamentalsData } from "@/lib/fundamentals";

function Box({ title, note }: { title: string; note: string }) {
  return <div className="ndbox"><div className="nd">{title}</div><p>{note}</p></div>;
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="fmetric"><div className="label">{label}</div><div className="v num">{value}</div></div>;
}

export function FundamentalsList({ data, loading, error, symbol }: {
  data: FundamentalsData | null; loading: boolean; error: string | null; symbol: string;
}) {
  if (loading) return <Box title="LOADING" note="Fetching company fundamentals…" />;
  if (error) return <Box title={NO_DATA} note="Fundamentals unavailable — nothing is shown, nothing invented." />;
  if (!hasFundamentals(data))
    return <Box title={NO_DATA} note="No fundamentals coverage for this symbol yet. Company quality appears once a fundamentals provider is connected — never fabricated." />;

  const d = data as FundamentalsData;
  const f = d.financials;
  const v = d.valuation;
  const est = d.analyst_estimates;
  const q = d.quality_score;

  return (
    <div className="fund">
      <div className="fhead">
        <div>
          <div className="label">{symbol} Fundamentals</div>
          <div className="fsub">{d.company?.company_name || symbol}{d.company?.sector ? ` · ${d.company.sector}` : ""}</div>
        </div>
        <div className="fscore">
          <div className="label" style={{ textAlign: "right" }}>Company Quality</div>
          <div className={`fq ${qualityTier(q)}`}>{q == null ? NO_DATA : q}<small>{q == null ? "" : " / 100"}</small></div>
        </div>
      </div>

      <div className="fgrid">
        <Metric label="Revenue Growth" value={pct(f?.revenue_growth)} />
        <Metric label="Gross Margin" value={pct(f?.gross_margin)} />
        <Metric label="Operating Margin" value={pct(f?.operating_margin)} />
        <Metric label="Net Margin" value={pct(f?.net_margin)} />
        <Metric label="EPS" value={isPresent(f?.eps) ? (f!.eps as number).toFixed(2) : NO_DATA} />
        <Metric label="Revenue" value={big(f?.revenue)} />
        <Metric label="Market Cap" value={big(v?.market_cap)} />
        <Metric label="P/E" value={isPresent(v?.pe_ratio) ? (v!.pe_ratio as number).toFixed(1) : NO_DATA} />
        <Metric label="Price / Sales" value={isPresent(v?.price_sales) ? (v!.price_sales as number).toFixed(1) : NO_DATA} />
        <Metric label="Valuation" value={<span className={`fval ${valuationTone(v?.pe_ratio)}`}>{valuationLabel(v?.pe_ratio)}</span>} />
      </div>

      <div className="fanalyst">
        <div className="label">Analyst Consensus</div>
        <div className="fameta">
          <span>Rating <b>{est?.rating || NO_DATA}</b></span>
          <span>Target <b>{isPresent(est?.target_price) ? big(est!.target_price) : NO_DATA}</b></span>
          <span>Analysts <b>{isPresent(est?.analyst_count) ? String(est!.analyst_count) : NO_DATA}</b></span>
        </div>
      </div>

      {(d.strengths.length || d.risks.length) ? (
        <div className="fsr">
          <div className="fcol">
            <div className="label">Strengths</div>
            {d.strengths.length ? d.strengths.map((s) => <div className="fitem pos" key={s}>✓ {s}</div>)
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

export function FundamentalsFeed({ symbol }: { symbol: string }) {
  const [state, setState] = useState<{ data: FundamentalsData | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, loading: true, error: null }));
    fetchFundamentals(symbol, ctrl.signal)
      .then((r) => { if (!cancelled) setState({ data: r, loading: false, error: null }); })
      .catch((e: any) => {
        if (cancelled || e?.name === "AbortError") return;
        setState({ data: null, loading: false, error: e?.message === "NO_BACKEND" ? "no backend configured" : "unavailable" });
      });
    return () => { cancelled = true; ctrl.abort(); };
  }, [symbol]);
  return <FundamentalsList data={state.data} loading={state.loading} error={state.error} symbol={symbol} />;
}
