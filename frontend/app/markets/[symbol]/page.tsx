"use client";
import { useParams } from "next/navigation";
import { useDashboard } from "@/components/shell";
import { MarketDetailView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  const params = useParams();
  const symbol = decodeURIComponent(String(params?.symbol ?? ""));
  return <MarketDetailView s={data} symbol={symbol} connected={connected} />;
}
