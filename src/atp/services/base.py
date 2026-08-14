"""Phase C service framework — shared plumbing for the 3 supervised runtime processes.

Every service:
  * connects to the authoritative Store (PostgreSQL) and the Bus (Redis, non-authoritative);
  * runs ``LifecycleManager.recover()`` at startup — an unexpected restart NEVER auto-resumes RUNNING;
  * emits a heartbeat row to ``service_heartbeats`` on a fixed interval;
  * exposes ``/health`` and ``/ready`` over loopback HTTP (readiness gates on DB reachability);
  * shuts down cleanly on SIGTERM/SIGINT.

Supervision is systemd (``User=atp``) — no terminal / Claude / Mac is required to keep them running.
The DB is the source of truth for supervision too: even if a process wedges, its heartbeat goes
stale and the Control service reports it unhealthy.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..runtime.lifecycle import LifecycleManager
from ..store import open_store
from .bus import Bus, open_bus


# ---------------------------------------------------------------- connection config (from env/atp.env)
def build_dsn() -> str:
    """Production-paper DSN. ``ATP_DATABASE_URL`` wins; otherwise built from the atp.env app creds."""
    url = os.environ.get("ATP_DATABASE_URL")
    if url:
        return url
    user = os.environ.get("ATP_APP_USER", "atp_app")
    pw = os.environ["ATP_APP_PASSWORD"]                        # required — fail loudly if absent
    db = os.environ.get("ATP_PROD_DB", "atp_prod")
    host = os.environ.get("ATP_PG_HOST", "127.0.0.1")
    port = os.environ.get("ATP_PG_PORT", "5432")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def redis_url() -> str | None:
    """Redis bus URL. ``ATP_REDIS_URL`` wins; otherwise built from ``REDIS_PASSWORD``.
    Returns None only if explicitly disabled (``ATP_REDIS_URL=``)."""
    if "ATP_REDIS_URL" in os.environ:
        return os.environ["ATP_REDIS_URL"] or None
    pw = os.environ.get("REDIS_PASSWORD")
    host = os.environ.get("ATP_REDIS_HOST", "127.0.0.1")
    port = os.environ.get("ATP_REDIS_PORT", "6379")
    return f"redis://:{pw}@{host}:{port}/0" if pw else f"redis://{host}:{port}/0"


# ---------------------------------------------------------------- health HTTP endpoint (loopback)
class _HealthHandler(BaseHTTPRequestHandler):
    def _write(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                      # noqa: N802 (http.server API)
        svc: Service = self.server.service         # type: ignore[attr-defined]
        snap = svc.health_snapshot()
        path = self.path.rstrip("/") or "/health"
        if path == "/health":
            self._write(200, snap)
        elif path == "/ready":
            self._write(200 if snap["ready"] else 503, snap)
        else:
            self._write(404, {"error": "not found"})

    def log_message(self, *_a) -> None:            # silence default stderr access log
        pass


# ---------------------------------------------------------------- base service
class Service:
    """Base class for a supervised Phase C runtime process. Subclasses override the lifecycle hooks."""

    name: str = "service"
    health_port: int = 9100
    heartbeat_interval: float = 5.0

    def __init__(self) -> None:
        # migrate=False: the schema is migrated once at deploy; services must not race on DDL.
        self.store = open_store(build_dsn(), migrate=False)
        self.bus: Bus = open_bus(redis_url())
        self.life = LifecycleManager(self.store)
        self._ready = False
        self._detail = "starting"
        self._stop = asyncio.Event()
        self._httpd: ThreadingHTTPServer | None = None

    # -- health ----------------------------------------------------------
    def _runtime_state(self) -> str | None:
        try:
            rs = self.store.get_runtime_state()
            return rs.status if rs else None
        except Exception:
            return None

    def health_snapshot(self) -> dict:
        try:
            db = self.store.ping()
        except Exception:
            db = False
        ready = self._ready and db
        return {
            "service": self.name,
            "status": "UP" if ready else "DEGRADED",
            "ready": ready,
            "db": db,
            "detail": self._detail,
            "runtime_state": self._runtime_state(),
        }

    def _start_health_server(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", self.health_port), _HealthHandler)
        httpd.service = self                       # type: ignore[attr-defined]
        threading.Thread(target=httpd.serve_forever, daemon=True, name=f"{self.name}-health").start()
        self._httpd = httpd

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            snap = self.health_snapshot()
            try:
                self.store.upsert_heartbeat(
                    service=self.name, status=snap["status"],
                    detail=(snap["detail"] or "")[:200] or None,
                )
            except Exception:
                pass                                # DB down: heartbeat simply goes stale (detected as unhealthy)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    # -- lifecycle hooks (override in subclasses) ------------------------
    async def on_start(self) -> None:
        """Startup work after recover() + health server are up (subscriptions, recovery checks)."""

    async def main(self) -> None:
        """The service's run loop. Default: idle until stopped."""
        await self._stop.wait()

    async def on_stop(self) -> None:
        """Cleanup before shutdown."""

    # -- run -------------------------------------------------------------
    async def _run(self) -> None:
        status = self.life.recover()               # CRITICAL: never auto-RUNNING
        self._detail = f"recovered:{status.value}"
        self._start_health_server()
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            await self.on_start()
            self._ready = True
            self._detail = "ready"
            await self.main()
        finally:
            self._ready = False
            self._detail = "stopping"
            self._stop.set()
            try:
                await self.on_stop()
            finally:
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass
                if self._httpd is not None:
                    self._httpd.shutdown()
                try:
                    await self.bus.close()
                except Exception:
                    pass
                self.store.close()

    def run(self) -> None:
        """Blocking entrypoint for ``python -m atp.services.<name>`` under systemd."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def _request_stop(*_a) -> None:
            loop.call_soon_threadsafe(self._stop.set)

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        try:
            loop.run_until_complete(self._run())
        finally:
            loop.close()
