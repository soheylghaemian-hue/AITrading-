"""AI Consensus (§ Phase G3) — the intelligence orchestration layer, READ-ONLY.

Combines the independent intelligence layers (Market Data / News / Fundamentals / Options / Trader
Intelligence / Risk) into ONE transparent AI market view: conviction score, direction, confidence,
per-source components, strengths, risks and — crucially — surfaced DISAGREEMENTS (conflicts), never
hidden. This is a deterministic first version (no black-box AI) and produces an INTELLIGENCE SIGNAL
only: NOT a trading decision, NOT execution, NOT order generation, NOT broker interaction.

Missing components render as NO DATA and the weights renormalize over what is present; too few inputs
→ PARTIAL ASSESSMENT (the score is not forced). Nothing is fabricated — every number traces to a real
intelligence layer. No Trading Core / Risk-engine-logic / Broker / IBKR / Execution code is touched.
"""
