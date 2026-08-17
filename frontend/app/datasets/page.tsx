"use client";
// Research Datasets page (§ Phase R3.0A) — RESEARCH DATA ONLY, not live trading. It builds and inspects
// immutable, checksum-verified historical OHLC datasets; it never trades, never places an order, never
// enables execution, and never touches live ohlc_bars.
import { useDashboard } from "@/components/shell";
import { Datasets } from "@/components/Datasets";

export default function Page() {
  const { connected } = useDashboard();
  return <Datasets connected={connected} />;
}
