"""News intelligence (§ Phase G2.1) — a READ-ONLY layer, fully isolated from trading.

Real headlines from the news provider → collector → PostgreSQL (news_items) → Control API → terminal.
Sentiment/impact are deterministic transforms of the REAL article text (or the provider's own
per-ticker sentiment when present) — never fabricated. No Trading Core / Risk / Broker / IBKR /
Execution / OHLC code is touched. When no provider is configured (or it returns nothing), nothing is
persisted and the terminal shows NO DATA.
"""
