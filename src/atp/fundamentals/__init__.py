"""Fundamentals intelligence (§ Phase G2.2) — a READ-ONLY intelligence input, fully isolated.

Company quality, financial health, valuation and growth for the AI Brain. This is NOT trading logic
and NOT execution — it is an intelligence input only.

Pipeline: provider → collector → PostgreSQL (companies / financial_metrics / valuation /
analyst_estimates) → quality engine (deterministic 0-100 + strengths/risks) → Control API → terminal.
Earnings / revenue / margins / valuation / analyst data are NEVER fabricated; the quality score and
strengths/risks are deterministic transforms of the real persisted metrics. No provider configured (or
one that returns nothing) → nothing persisted → NO DATA. No broker/IBKR/execution access, no credentials.
"""
