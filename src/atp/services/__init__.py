"""Phase C — supervised runtime services (§ service separation).

Three independently supervised processes replace the single-process dev runtime:

  * ``atp.services.marketdata`` — Market Data Service (fixture/real feed → validated quotes on the bus)
  * ``atp.services.trading``    — Trading Core + Execution Service (intents, risk, idempotency)
  * ``atp.services.control``    — Control / Observability Service (FastAPI health + control commands)

PostgreSQL (``atp.store``) is the single source of truth; the bus (``atp.services.bus``) is Redis
pub/sub used as transport/cache ONLY — never authoritative. Every process runs
``LifecycleManager.recover()`` at startup and can never auto-resume RUNNING.
"""
