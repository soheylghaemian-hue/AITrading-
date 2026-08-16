"use client";
import React, { useState } from "react";
import { useDashboard } from "@/components/shell";
import { AiBrainView } from "@/components/views";
import { ConsensusPanel } from "@/components/ConsensusPanel";
import { DataQualityPanel } from "@/components/DataQualityPanel";
import { MacroPanel } from "@/components/MacroPanel";
import { GovernancePanel } from "@/components/GovernancePanel";
import { AiPerformancePanel } from "@/components/AiPerformance";

const SYMBOLS = ["NVDA", "AAPL", "SPY"];

export default function Page() {
  const { data, connected } = useDashboard();
  const [symbol, setSymbol] = useState("NVDA");
  return (
    <>
      <div className="csymbar">
        <span className="label">AI Market View</span>
        <div className="strip">
          {SYMBOLS.map((s) => (
            <button key={s} className={`pill${symbol === s ? " ivon" : ""}`} onClick={() => setSymbol(s)}
              style={{ cursor: "pointer", font: "inherit" }} aria-pressed={symbol === s}>{s}</button>
          ))}
        </div>
      </div>
      <ConsensusPanel symbol={symbol} />
      <DataQualityPanel symbol={symbol} />
      <MacroPanel />
      <GovernancePanel symbol={symbol} />
      <AiPerformancePanel />
      <AiBrainView s={data} connected={connected} />
    </>
  );
}
