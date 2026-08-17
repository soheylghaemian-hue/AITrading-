"use client";
// § R3.1A — AI Validation research page (RESEARCH DATA ONLY, not live trading). Forward-only immutable
// point-in-time collection + deterministic prediction-quality validation; never trades, never an order.
import { useDashboard } from "@/components/shell";
import { Validation } from "@/components/Validation";

export default function Page() {
  const { connected } = useDashboard();
  return <Validation connected={connected} />;
}
