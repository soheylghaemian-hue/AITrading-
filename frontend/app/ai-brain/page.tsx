"use client";
import { useDashboard } from "@/components/shell";
import { AiBrainView } from "@/components/views";

export default function Page() {
  const { data, connected } = useDashboard();
  return <AiBrainView s={data} connected={connected} />;
}
