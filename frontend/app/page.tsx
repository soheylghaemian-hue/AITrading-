"use client";
import { useDashboard } from "@/components/shell";
import { OverviewView } from "@/components/views";
import { OverviewReadiness } from "@/components/OverviewReadiness";

export default function Page() {
  const { data, connected } = useDashboard();
  return (
    <>
      <OverviewReadiness snapshot={data} connected={connected} />
      <OverviewView s={data} connected={connected} />
    </>
  );
}
