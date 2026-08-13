import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TradingRisk } from "../components/TradingRisk";
import type { Snapshot } from "../lib/types";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

const active: Snapshot = { trading_risk: {
  capital: 1_000_000, risk_per_trade_pct: 0.01, max_risk_per_trade: 10_000,
  max_daily_loss_pct: 0.02, max_daily_loss: 20_000, current_daily_pnl: -5_000,
  remaining_daily_risk: 15_000, status: "ACTIVE",
} };

describe("TRADING RISK panel", () => {
  it("shows the three parameters and the derived monetary limits (example values)", () => {
    const html = r(<TradingRisk s={active} />);
    expect(html).toContain("$1,000,000");   // capital
    expect(html).toContain("1.0%");          // risk per trade
    expect(html).toContain("$10,000");       // max risk / trade
    expect(html).toContain("2.0%");          // daily loss limit
    expect(html).toContain("$20,000");       // max daily loss
    expect(html).toContain("$15,000");       // remaining daily risk
    expect(html).toContain("ACTIVE");
  });

  it("shows the three input controls and no manual size/leverage/exposure controls", () => {
    const html = r(<TradingRisk s={active} />);
    expect((html.match(/type="number"/g) || []).length).toBe(3);
    expect(html.toLowerCase()).toContain("trading capital");
    expect(html.toLowerCase()).toContain("risk per trade");
    expect(html.toLowerCase()).toContain("max daily loss");
    expect(html.toLowerCase()).not.toContain("position size");
    expect(html.toLowerCase()).not.toContain("leverage (");
  });

  it("DAILY LOSS LIMIT REACHED status renders as a halt state", () => {
    const reached: Snapshot = { trading_risk: { ...active.trading_risk!, status: "DAILY LOSS LIMIT REACHED", remaining_daily_risk: 0 } };
    expect(r(<TradingRisk s={reached} />)).toContain("DAILY LOSS LIMIT REACHED");
  });

  it("with no backend data shows NO DATA, never fabricated numbers", () => {
    const html = r(<TradingRisk s={null} />);
    expect(html).toContain("NO DATA");
    expect(html).not.toContain("$0");
  });
});
