"""Trader intelligence (§ Phase G2.5) — a READ-ONLY intelligence input source, fully isolated.

Understand what skilled market participants are doing and turn it into ONE signal among many for the
AI Brain. This is NOT copy-trading and NOT execution: "high-quality trader behaviour becomes an
intelligence signal", never "trader buys NVDA → GIGBAY buys NVDA".

Pipeline: provider → collector → PostgreSQL (traders / trader_performance / trader_positions) →
quality engine (deterministic 0-100 score) → consensus engine (quality-weighted per symbol) →
Control API → terminal. No credentials, no broker/IBKR/execution access. No provider configured (or a
provider that returns nothing) → nothing persisted → NO DATA. Returns/positions/performance/consensus
are NEVER fabricated; quality and consensus are deterministic transforms of real persisted data.
"""
