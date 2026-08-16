"""Options-flow intelligence (§ Phase G2.3) — a READ-ONLY intelligence input, fully isolated.

NOTE: this is the options *intelligence* layer (positioning / IV / unusual activity as an AI-Brain
signal). It is distinct from `atp.options` (the Black-Scholes pricing / greeks / combo-execution
package). This package is named `optflow` to avoid any collision with that pricing module.

Market positioning, volatility, institutional activity and options sentiment for the AI Brain — NOT
trading logic, NOT execution, NOT order generation, NOT broker interaction.

Pipeline: provider → collector → PostgreSQL (options_snapshot / options_flow) → analytics engine
(deterministic 0-100 score + signals/risks) → Control API → terminal. IV / volume / open interest /
option flow / unusual activity are NEVER fabricated. No provider / no entitlement → NO DATA. No broker/
IBKR/execution access, no credentials.
"""
