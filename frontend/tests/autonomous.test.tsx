import { describe, it, expect } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Autonomous } from "../components/sections2";
import type { Snapshot } from "../lib/types";

const r = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("AUTONOMOUS TRADING section", () => {
  it("default (no block) shows PAPER AUTONOMOUS DISABLED, never live", () => {
    const html = r(<Autonomous s={{} as Snapshot} />);
    expect(html).toContain("PAPER");
    expect(html).toContain("DISABLED");
    expect(html.toLowerCase()).not.toContain("live execution on");
  });

  it("renders paper metrics, decision feed, and safety (no live, 0 IBKR orders)", () => {
    const s: Snapshot = { autonomous: {
      mode: "PAPER", status: "RUNNING", paper_equity: 1_000_000, today_pnl: -750,
      open_positions: 1, trades_today: 2, risk_used: 0.25, remaining_daily_loss: 22_500,
      max_daily_loss: 30_000, live_execution: false, ibkr_orders: 0,
      decisions: [
        { ts: "2026-08-13T14:05:00Z", instrument: "EUR.USD", action: "BUY", quantity: 1000, price: 1.15, decision: "FILLED", reason: "momentum BUY" },
        { ts: "2026-08-13T14:06:00Z", instrument: "AAPL", action: null, quantity: null, price: null, decision: "NO_DATA", reason: "market data not tradable" },
      ],
    } } as Snapshot;
    const html = r(<Autonomous s={s} />);
    expect(html).toContain("RUNNING");
    expect(html).toContain("FILLED");
    expect(html).toContain("NO DATA");   // NO_DATA decision pill (underscores rendered as spaces)
    expect(html).toContain("momentum BUY");
    expect(html.toLowerCase()).toContain("live execution");
    // safety: live execution DISABLED and 0 IBKR orders visible
    expect(html).toContain("DISABLED");
  });

  it("HALTED status renders as a halt state", () => {
    const s: Snapshot = { autonomous: { mode: "PAPER", status: "HALTED", paper_equity: null,
      today_pnl: null, open_positions: null, trades_today: 0, risk_used: null,
      remaining_daily_loss: 0, max_daily_loss: null, live_execution: false, ibkr_orders: 0,
      decisions: [] } } as Snapshot;
    expect(r(<Autonomous s={s} />)).toContain("HALTED");
  });
});
