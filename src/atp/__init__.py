"""atp — Autonomous Multi-Asset Trading platform.

A modular, broker-agnostic trading desk built from the Gesamtkonzept V1.0:
market data → features → regime → specialist signals → opportunity ranking →
portfolio/sizing → risk veto → execution → broker, wrapped by backtesting,
validation and (later) learning/governance.

Nothing here promises profit. It provides the *machinery* to research, validate and — only
after paper trading and explicit policy — autonomously execute strategies under an
independent Risk Engine (§14/§25).
"""

__version__ = "0.1.0"
