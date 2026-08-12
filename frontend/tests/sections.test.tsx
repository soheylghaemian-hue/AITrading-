import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { KpiGrid, MarketData, Performance, AiAnalysis } from "../components/sections";
import type { Snapshot } from "../lib/types";

const render = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("dashboard renders without a backend (NO DATA)", () => {
  it("KpiGrid with null snapshot shows NO DATA, never fake 0", () => {
    const html = render(<KpiGrid s={null} />);
    expect(html).toContain("NO DATA");
    expect(html).toContain("Equity");
  });

  it("MarketData with null snapshot shows NO DATA and no fabricated prices", () => {
    const html = render(<MarketData s={null} />);
    expect(html).toContain("NO DATA");
  });

  it("Performance shows NO DATA until a real closed trade exists (no fake P&L)", () => {
    const html = render(<Performance s={{ n_trades: 0, analytics_overall: {} } as Snapshot} />);
    expect(html).toContain("NO DATA");
    expect(html).not.toContain("Win rate"); // the metric rows are hidden until real trades
  });
});

describe("market-data states render distinctly and honestly", () => {
  const s: Snapshot = {
    system_health: { market_data: "degraded" },
    market_data: [
      { symbol: "EUR.USD", asset_class: "fx", exchange: "IDEALPRO", status: "DATA_AVAILABLE",
        market_data_type: "REALTIME", bid: 1.15234, ask: 1.15235, last: null, reason: "live" },
      { symbol: "AAPL", asset_class: "equity", exchange: "NASDAQ", status: "DATA_NOT_AVAILABLE",
        market_data_type: null, bid: null, ask: null, last: null, error_code: 10089,
        reason: "IBKR market-data subscription required" },
    ],
  };

  it("REALTIME row shows real bid/ask + the REALTIME type", () => {
    const html = render(<MarketData s={s} />);
    expect(html).toContain("REALTIME");
    expect(html).toContain("1.15234");
    expect(html).toContain("1.15235");
  });

  it("NOT_AVAILABLE row is shown with reason, and no fabricated price", () => {
    const html = render(<MarketData s={s} />);
    expect(html.toLowerCase()).toContain("subscription required");
    expect(html).toContain("DATA NOT AVAILABLE"); // pill text (underscores replaced)
    // AAPL had null bid/ask → must not print a 0 price
    expect(html).not.toMatch(/AAPL[\s\S]*?>0<\/td>/);
  });

  it("DELAYED is a distinct state, never shown as realtime", () => {
    const delayed: Snapshot = { market_data: [
      { symbol: "X", status: "DELAYED", market_data_type: "DELAYED", bid: 10, ask: 10.1, last: 10 } as any,
    ] };
    const html = render(<MarketData s={delayed} />);
    expect(html).toContain("DELAYED");
  });
});

describe("AI analysis never fabricates a signal from missing data", () => {
  it("NO DATA rows render as NO DATA (no invented action/confidence)", () => {
    const s: Snapshot = { ai_analysis: [
      { agent: "cross_asset", instrument: "*", status: "NO DATA", action: null, confidence: null,
        reason: "requires data not sourced" },
    ] };
    const html = render(<AiAnalysis s={s} />);
    expect(html).toContain("NO DATA");
  });
});
