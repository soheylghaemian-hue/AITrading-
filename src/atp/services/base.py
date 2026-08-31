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
import concurrent.futures
import hmac
import json
import os
import signal
import threading
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any

from ..runtime.lifecycle import LifecycleManager
from ..store import open_store
from .bus import Bus, open_bus

PAPER_CANARY_INTERNAL_TOKEN_HEADER = "X-ATP-Paper-Canary-Token"
PAPER_CANARY_COMMAND_BODY_LIMIT = 16_384


class LoopbackCommandError(RuntimeError):
    """A deliberately bounded error returned by the private loopback command adapter."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _LoopbackCommandHandler(BaseHTTPRequestHandler):
    """Private JSON-only adapter; command execution is always marshalled onto the owner loop."""

    def setup(self) -> None:
        super().setup()
        adapter: LoopbackCommandServer = self.server.adapter  # type: ignore[attr-defined]
        self.connection.settimeout(adapter.request_timeout)

    def _write(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._write(404, {"detail": "not found"})

    def do_POST(self) -> None:
        adapter: LoopbackCommandServer = self.server.adapter  # type: ignore[attr-defined]
        if self.path not in adapter.paths:
            self._write(404, {"detail": "not found"})
            return
        supplied = self.headers.get(PAPER_CANARY_INTERNAL_TOKEN_HEADER, "")
        if not hmac.compare_digest(adapter.token, supplied):
            self._write(401, {"detail": "unauthorized"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._write(400, {"detail": "transfer encoding is not supported"})
            return
        if self.headers.get("Content-Type") != "application/json":
            self._write(415, {"detail": "content type must be application/json"})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            self._write(400, {"detail": "a valid content length is required"})
            return
        if length > adapter.body_limit:
            self._write(413, {"detail": "request body too large"})
            return
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._write(408, {"detail": "request body timed out"})
            return
        if len(raw) != length:
            self._write(400, {"detail": "incomplete request body"})
            return

        def _unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._write(400, {"detail": "malformed JSON object"})
            return
        if type(payload) is not dict:
            self._write(400, {"detail": "request body must be a JSON object"})
            return
        future = asyncio.run_coroutine_threadsafe(
            adapter.handler(self.path, payload), adapter.owner_loop,
        )
        try:
            result = future.result(timeout=adapter.command_timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._write(504, {"detail": "owner command timed out"})
        except LoopbackCommandError as exc:
            self._write(exc.status_code, {"detail": exc.detail})
        except Exception:  # noqa: BLE001 - private boundary must map every owner failure to fail-closed
            self._write(500, {"detail": "owner command failed closed"})
        else:
            self._write(200, {"ok": True, "result": result})

    def log_message(self, *_a) -> None:
        pass


class LoopbackCommandServer:
    """A loopback-only HTTP thread that can never execute a command off the supplied event loop."""

    def __init__(
        self,
        *,
        owner_loop: asyncio.AbstractEventLoop,
        handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        token: str,
        paths: frozenset[str],
        port: int,
        body_limit: int = PAPER_CANARY_COMMAND_BODY_LIMIT,
        command_timeout: float = 30.0,
        request_timeout: float = 10.0,
    ) -> None:
        if type(token) is not str or not token:
            raise ValueError("loopback command token must be a non-empty string")
        if type(port) is not int or not 0 <= port <= 65_535:
            raise ValueError("loopback command port is invalid")
        if type(body_limit) is not int or body_limit <= 0:
            raise ValueError("loopback command body limit is invalid")
        if type(command_timeout) not in {int, float} or not 0 < command_timeout <= 300:
            raise ValueError("loopback command timeout is invalid")
        if type(request_timeout) not in {int, float} or not 0 < request_timeout <= 60:
            raise ValueError("loopback request timeout is invalid")
        if not paths or any(type(path) is not str or not path.startswith("/") for path in paths):
            raise ValueError("loopback command paths are invalid")
        self.owner_loop = owner_loop
        self.handler = handler
        self.token = token
        self.paths = paths
        self.body_limit = body_limit
        self.command_timeout = float(command_timeout)
        self.request_timeout = float(request_timeout)
        self._httpd = HTTPServer(("127.0.0.1", port), _LoopbackCommandHandler)
        self._httpd.adapter = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("loopback command server is closed")
        if self._thread is not None:
            raise RuntimeError("loopback command server already started")
        thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="paper-canary-loopback",
        )
        thread.start()
        self._thread = thread

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is None:
            self._httpd.server_close()
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        thread.join(timeout=5)
        self._thread = None


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
