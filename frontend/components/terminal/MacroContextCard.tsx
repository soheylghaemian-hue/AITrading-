"use client";
// Macro context card (§ Phase R1.2) — a compact read-out, in the Market Terminal, of what the current
// global macro regime means for THIS symbol: TAILWIND / NEUTRAL / HEADWIND plus the regime, score and
// top drivers/risks. Read-only intelligence context; never a trade. No snapshot → NO DATA.
import React from "react";
import { NO_DATA } from "@/lib/format";
import { hasMacro, regimeLabel, regimeTone, relevanceTone, type MacroContext } from "@/lib/macro";

export function MacroContextCard({ data }: { data: MacroContext | null }) {
  if (!hasMacro(data)) {
    return (
      <div className="card macroctx">
        <div className="mctx-head"><span className="label">Macro Context</span><span className="mctx-nd">{NO_DATA}</span></div>
        <p className="mctx-note">No macro reading yet — appears once a macro provider is configured. Never fabricated.</p>
      </div>
    );
  }
  const d = data as MacroContext;
  return (
    <div className={`card macroctx ${regimeTone(d.regime)}`}>
      <div className="mctx-head">
        <span className="label">Macro Context</span>
        <span className={`regimeb ${regimeTone(d.regime)}`}>{regimeLabel(d.regime)}</span>
        <span className={`mctx-rel ${relevanceTone(d.relevance)}`}>{d.relevance ?? NO_DATA}</span>
        <span className="mctx-score num">{d.score == null ? NO_DATA : `${d.score}/100`}</span>
      </div>
      {d.note ? <p className="mctx-note">{d.note}</p> : null}
      <div className="mctx-lists">
        {d.signals.slice(0, 3).map((s) => <span className="mctx-sig pos" key={s}>✓ {s}</span>)}
        {d.risks.slice(0, 3).map((s) => <span className="mctx-sig neg" key={s}>⚠ {s}</span>)}
      </div>
    </div>
  );
}
