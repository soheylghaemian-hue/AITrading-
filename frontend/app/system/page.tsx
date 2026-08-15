"use client";
import { useDashboard } from "@/components/shell";
import { SystemView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  return <SystemView s={data} connected={connected} />;
}
