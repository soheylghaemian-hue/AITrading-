"use client";
import { useParams } from "next/navigation";
import { useDashboard } from "@/components/shell";
import { MarketTerminal } from "@/components/terminal/MarketTerminal";

export default function Page() {
  const { data, connected } = useDashboard();
  const params = useParams();
  const symbol = decodeURIComponent(String(params?.symbol ?? ""));
  return <MarketTerminal s={data} symbol={symbol} connected={connected} />;
}
