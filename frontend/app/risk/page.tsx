"use client";
import { useDashboard } from "@/components/shell";
import { RiskView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  return <RiskView s={data} connected={connected} />;
}
