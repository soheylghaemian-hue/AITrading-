"use client";
// Backtesting Research page (§ Phase R3.0) — RESEARCH ONLY, not live trading. It starts internal
// historical research runs; it never enables trading, never places an order, never touches execution.
import { useDashboard } from "@/components/shell";
import { Backtesting } from "@/components/Backtesting";

export default function Page() {
  const { connected } = useDashboard();
  return <Backtesting connected={connected} />;
}
