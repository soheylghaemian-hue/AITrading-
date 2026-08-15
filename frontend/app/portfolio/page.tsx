"use client";
import { useDashboard } from "@/components/shell";
import { PortfolioView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  return <PortfolioView s={data} connected={connected} />;
}
