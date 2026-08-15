"use client";
import { useDashboard } from "@/components/shell";
import { MarketsView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  return <MarketsView s={data} connected={connected} />;
}
