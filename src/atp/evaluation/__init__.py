"""AI evaluation & performance tracking (§ Phase G3.1) — READ-ONLY, honest.

Measures whether the AI consensus is PREDICTIVE. It only evaluates predictions — it is NOT trading
execution, strategy activation, or order generation.

Flow: AI consensus → immutable prediction snapshot (ai_predictions) → outcome tracker (measures the
1/3/5/20-day forward return from real OHLC) → deterministic evaluation (accuracy / directional accuracy
/ average return / confidence calibration / error classification). History is NEVER rewritten: old
scores never change and failed predictions are never removed — the AI is evaluated honestly. No Trading
Core / Risk / Broker / IBKR / Execution code is touched; missing outcomes render as NO DATA.
"""
