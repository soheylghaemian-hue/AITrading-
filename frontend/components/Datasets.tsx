"use client";
// Research Datasets page (§ Phase R3.0A). RESEARCH DATA ONLY — an immutable, versioned, checksum-verified
// OHLC dataset built from split-adjusted 1-minute aggregates normalized to regular-session (RTH) daily bars.
// It never trades, never places or submits an order, never enables execution, and never touches live
// ohlc_bars. Every field traces to a real dataset record or renders NO DATA / MISSING (never fabricated).
import React, { useEffect, useState } from "react";
import { NO_DATA } from "@/lib/format";
import { createDataset, fetchDatasets, fetchDataset, fetchDatasetCoverage, POLL_MS } from "@/lib/api";
import {
  type ResearchDataset, type DatasetCoverage, datasetTone, rangeText, shortChecksum,
} from "@/lib/dataset";

function SafetyBanner() {
  return (
    <div className="bt-banner">
      <span className="bt-b-tag">RESEARCH DATA ONLY</span>
      <span className="bt-b-tag">IMMUTABLE · CHECKSUM-VERIFIED</span>
      <span className="bt-b-pill">AUTONOMOUS <b>DISABLED</b></span>
      <span className="bt-b-pill">EXECUTION <b>DISABLED</b></span>
      <span className="bt-b-pill">IBKR ORDERS <b>0</b></span>
    </div>
  );
}

function BuildForm({ connected, onBuilt }: { connected: boolean; onBuilt: () => void }) {
  const [symbols, setSymbols] = useState("NVDA, AAPL");
  const [start, setStart] = useState("2023-01-03");
  const [end, setEnd] = useState("2023-06-30");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "err" | "disabled"; text: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    const syms = symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (!syms.length) { setMsg({ tone: "err", text: "at least one symbol is required" }); return; }
    setBusy(true);
    try {
      const r = await createDataset({ symbols: syms, interval: "1D", start, end });
      if (r.ok && r.data) {
        const s = r.data.status;
        const note = s === "COMPLETED" ? "already built (reused)" : "queued — the research worker will build it";
        setMsg({ tone: "ok", text: `${s} · ${r.data.dataset_id.slice(0, 12)}… — ${note}` });
        onBuilt();
      } else if (r.disabled) setMsg({ tone: "disabled", text: r.detail || "historical backfill is disabled on this server (enabled only after approval)" });
      else setMsg({ tone: "err", text: r.detail || "enqueue failed" });
    } finally { setBusy(false); }
  }

  return (
    <form className="card ds-build" onSubmit={submit}>
      <div className="ds-build-head">
        <b>Build a research dataset</b>
        <span className="bt-hint">US equities · 1D · split-adjusted 1-minute → RTH daily · approved universe NVDA/AAPL/SPY</span>
      </div>
      <div className="ds-build-grid">
        <label className="bt-f bt-wide"><span className="label">Symbols</span>
          <input value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="NVDA, AAPL, SPY" /></label>
        <label className="bt-f"><span className="label">Interval</span>
          <select value="1D" disabled><option value="1D">1D</option></select></label>
        <label className="bt-f"><span className="label">Start</span><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="bt-f"><span className="label">End</span><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
      </div>
      {msg ? <div className={`ds-msg ${msg.tone}`}>{msg.tone === "disabled" ? "🔒 " : msg.tone === "ok" ? "✓ " : "⚠ "}{msg.text}</div> : null}
      <div className="bt-actions">
        <button type="submit" className="bt-run" disabled={busy || !connected}>{busy ? "Building…" : "Build Dataset"}</button>
        <span className="bt-safe">Research data only · reads market data · never trades, never places an order, never touches live ohlc_bars</span>
      </div>
    </form>
  );
}

function DatasetDetail({ ds, coverage }: { ds: ResearchDataset; coverage: DatasetCoverage | null }) {
  const tone = datasetTone(ds.status);
  const cov = coverage?.per_symbol || [];
  const supersededBy = ds.superseded_by || [];
  return (
    <div className="ds-detail">
      <div className={`ds-detail-head ${tone}`}>
        <div><div className="label">Dataset</div><div className="num ds-id">{ds.dataset_id.slice(0, 16)}…</div></div>
        <div><div className="label">Status</div><div className={`bt-state ${tone}`}>{ds.status}</div></div>
        <div><div className="label">Symbols</div><div>{ds.symbols.join(", ")} · {ds.interval}</div></div>
        <div><div className="label">Bars</div><div className="num">{ds.row_count ?? NO_DATA}</div></div>
      </div>

      <div className="card">
        <div className="ds-meta">
          <div className="ds-m"><span className="label">Provider</span><b>{ds.provider ?? NO_DATA}</b></div>
          <div className="ds-m"><span className="label">Provider contract</span><b>{ds.provider_contract_version ?? NO_DATA}</b></div>
          <div className="ds-m"><span className="label">Adjustment policy</span><b>{ds.adjustment_policy ?? NO_DATA}</b></div>
          <div className="ds-m"><span className="label">Normalization</span><b>{ds.normalization_policy ?? NO_DATA}</b></div>
          <div className="ds-m"><span className="label">Calendar</span><b>{ds.calendar_version ?? NO_DATA}</b></div>
          <div className="ds-m"><span className="label">Date range</span><b className="num">{rangeText(ds)}</b></div>
          <div className="ds-m"><span className="label">Adjusted flag</span><b>{ds.provider_adjusted_flag == null ? NO_DATA : ds.provider_adjusted_flag ? "true (split-adjusted)" : "false"}</b></div>
          <div className="ds-m"><span className="label">Missing-minute threshold</span><b className="num">{ds.missing_minute_threshold ?? NO_DATA}</b></div>
          <div className="ds-m ds-m-wide"><span className="label">Dataset checksum</span><b className="num bt-ck">{ds.dataset_checksum ?? NO_DATA}</b></div>
          <div className="ds-m ds-m-wide"><span className="label">Raw-pages checksum</span><b className="num bt-ck">{ds.raw_pages_checksum ?? NO_DATA}</b></div>
        </div>
        {(ds.retry_of_dataset_id || ds.supersedes_dataset_id || supersededBy.length) ? (
          <div className="ds-lineage">
            {ds.retry_of_dataset_id ? <span className="ds-tag">retry of {ds.retry_of_dataset_id.slice(0, 10)}…</span> : null}
            {ds.supersedes_dataset_id ? <span className="ds-tag">supersedes {ds.supersedes_dataset_id.slice(0, 10)}…</span> : null}
            {supersededBy.map((x) => <span className="ds-tag warn" key={x}>superseded by {x.slice(0, 10)}…</span>)}
          </div>
        ) : null}
      </div>

      {ds.status === "FAILED" ? (
        <div className="card bt-failed">
          <div className="bt-fail-code">{ds.failure_code || "FAILED"}</div>
          <p>{ds.failure_reason || "The dataset build failed. No bars were fabricated."}</p>
        </div>
      ) : (
        <div className="card"><h3 style={{ marginBottom: 8 }}>Coverage</h3>
          {cov.length ? (
            <table className="bt-cov"><thead><tr><th>Symbol</th><th>Bars</th><th>First session</th><th>Last session</th></tr></thead>
              <tbody>{cov.map((c) => <tr key={c.symbol}><td>{c.symbol}</td><td className="num">{c.bar_count}</td>
                <td className="num">{c.first_ts ? c.first_ts.slice(0, 10) : NO_DATA}</td>
                <td className="num">{c.last_ts ? c.last_ts.slice(0, 10) : NO_DATA}</td></tr>)}</tbody></table>
          ) : <div className="nodata"><div className="nd">{NO_DATA}</div><p>No coverage rows.</p></div>}
          {ds.missing_data && Object.keys(ds.missing_data).length ? (
            <details className="ds-missing"><summary><span className="chev">▸</span>Missing / rejected sessions</summary>
              <pre className="num">{JSON.stringify(ds.missing_data, null, 2).slice(0, 4000)}</pre></details>
          ) : null}
        </div>
      )}

      {ds.events?.length ? (
        <div className="card"><h3 style={{ marginBottom: 8 }}>Build Events</h3>
          <div className="bt-events">{ds.events.map((e, i) => (
            <div className={`bt-ev ${e.severity === "ERROR" || e.severity === "CRITICAL" ? "blocked" : e.severity === "WARNING" ? "warning" : ""}`} key={i}>
              <span className="bt-ev-type">{e.event_type}</span>{e.symbol ? <span className="bt-ev-sym">{e.symbol}</span> : null}
              <span className="bt-ev-det num">{Object.keys(e.details || {}).length ? JSON.stringify(e.details).slice(0, 100) : ""}</span></div>))}</div>
        </div>
      ) : null}
    </div>
  );
}

export function Datasets({ connected }: { connected: boolean }) {
  const [datasets, setDatasets] = useState<ResearchDataset[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string>("");
  const [detail, setDetail] = useState<ResearchDataset | null>(null);
  const [coverage, setCoverage] = useState<DatasetCoverage | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let live = true;
    fetchDatasets()
      .then((r) => { if (live) setDatasets(r.datasets); })
      .catch(() => { if (live) setDatasets([]); })
      .finally(() => { if (live) setLoaded(true); });
    return () => { live = false; };
  }, [reloadKey]);

  // Poll the read endpoint so PLANNED → RUNNING → COMPLETED/FAILED transitions (driven by the external
  // worker) surface without a manual refresh. Never fabricates: on error the list simply holds.
  useEffect(() => {
    const id = setInterval(() => setReloadKey((k) => k + 1), Math.max(3000, POLL_MS));
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!selectedId) { setDetail(null); setCoverage(null); return; }
    let live = true;
    Promise.all([fetchDataset(selectedId).catch(() => null), fetchDatasetCoverage(selectedId).catch(() => null)])
      .then(([d, c]) => { if (live) { setDetail(d); setCoverage(c); } });
    return () => { live = false; };
  }, [selectedId, reloadKey]);

  return (
    <>
      {!connected ? <div className="banner"><span className="dot r" aria-hidden="true" />Live backend not reachable — showing&nbsp;<b>NO DATA</b>. No datasets are fabricated.</div> : null}
      <SafetyBanner />
      <BuildForm connected={connected} onBuilt={() => setReloadKey((k) => k + 1)} />

      <div className="grid k2 ds-layout">
        <div className="card ds-list-card">
          <h3 style={{ marginBottom: 10 }}>Datasets {loaded ? <span className="ds-count">{datasets.length}</span> : null}</h3>
          {loaded && datasets.length === 0 ? (
            <div className="nodata"><div className="nd">NO DATASETS</div>
              <p>Build an immutable research dataset above. A backtest can only run against an explicit dataset.</p></div>
          ) : (
            <ul className="ds-list">
              {datasets.map((d) => {
                const tone = datasetTone(d.status);
                return (
                  <li key={d.dataset_id}>
                    <button className={`ds-row ${d.dataset_id === selectedId ? "on" : ""}`} onClick={() => setSelectedId(d.dataset_id)}>
                      <span className={`ds-dot ${tone}`} aria-hidden="true" />
                      <span className="ds-row-main">
                        <span className="ds-row-sym">{d.symbols.join("/")} · {d.interval}</span>
                        <span className="ds-row-range num">{rangeText(d)}</span>
                      </span>
                      <span className="ds-row-meta">
                        <span className={`bt-state sm ${tone}`}>{d.status}</span>
                        <span className="ds-row-ck num">{shortChecksum(d.dataset_checksum, 8)}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div>
          {detail ? <DatasetDetail ds={detail} coverage={coverage} /> : (
            <div className="card"><div className="nodata"><div className="nd">SELECT A DATASET</div>
              <p>Choose a dataset to inspect its provider, adjustment policy, date range, coverage and checksum.</p></div></div>
          )}
        </div>
      </div>
    </>
  );
}
