"""Push sinks — forward NotificationCenter events to a phone (§23).

The backend (which owns the NotificationCenter) forwards each event to an external push service
the owner has on their iPhone: **ntfy** (fastest — install the app, pick a secret topic) or
**Telegram** (most private — a bot + your chat id). The public frontend is never involved and
holds no tokens; every secret lives only in the backend environment.

Stdlib only (urllib) — no new dependency. Delivery is best-effort and severity-filtered; a push
failure is logged and never interrupts trading.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Mapping, Protocol

from ..logging_config import get_logger
from .notifications import Notification, Severity

log = get_logger("notify")


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context. Prefers certifi's CA bundle when available (fixes the common
    macOS-Python 'unable to get local issuer certificate' case); otherwise the system default.
    Certificate verification is ALWAYS on — it is never disabled."""
    try:
        import certifi  # noqa: PLC0415 — optional; used only for its CA bundle
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()

_SEV_ORDER = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
_SEV_EMOJI = {Severity.INFO: "ℹ️", Severity.WARNING: "⚠️", Severity.CRITICAL: "🚨"}
_NTFY_PRIORITY = {Severity.INFO: "default", Severity.WARNING: "high", Severity.CRITICAL: "urgent"}


def passes(severity: Severity, minimum: Severity) -> bool:
    return _SEV_ORDER[severity] >= _SEV_ORDER[minimum]


def format_message(n: Notification) -> str:
    return f"{_SEV_EMOJI.get(n.severity, '')} [{n.severity.value.upper()}] {n.kind.value}: {n.message}".strip()


class PushSink(Protocol):
    def deliver(self, n: Notification) -> None: ...


# --------------------------------------------------------------------------- Telegram
def telegram_request(token: str, chat_id: str, n: Notification) -> tuple[str, bytes]:
    """Pure builder → (url, form-encoded body). No network. Testable."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": format_message(n)}).encode("utf-8")
    return url, body


class TelegramSink:
    def __init__(self, token: str, chat_id: str, *, min_severity: Severity = Severity.WARNING,
                 timeout: float = 5.0) -> None:
        self._token, self._chat_id, self._min, self._timeout = token, chat_id, min_severity, timeout

    def deliver(self, n: Notification) -> None:
        if not passes(n.severity, self._min):
            return
        url, body = telegram_request(self._token, self._chat_id, n)
        urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=self._timeout,
                               context=_ssl_context())


# --------------------------------------------------------------------------- ntfy
def ntfy_request(server: str, topic: str, n: Notification) -> tuple[str, bytes, dict]:
    """Pure builder → (url, body, headers). No network. Testable."""
    url = f"{server.rstrip('/')}/{urllib.parse.quote(topic)}"
    headers = {
        "Title": n.kind.value.replace("_", " "),
        "Priority": _NTFY_PRIORITY.get(n.severity, "default"),
        "Tags": n.severity.value,
    }
    return url, n.message.encode("utf-8"), headers


class NtfySink:
    def __init__(self, topic: str, *, server: str = "https://ntfy.sh",
                 min_severity: Severity = Severity.WARNING, token: str | None = None,
                 timeout: float = 5.0) -> None:
        self._topic, self._server, self._min = topic, server, min_severity
        self._token, self._timeout = token, timeout

    def deliver(self, n: Notification) -> None:
        if not passes(n.severity, self._min):
            return
        url, body, headers = ntfy_request(self._server, self._topic, n)
        req = urllib.request.Request(url, data=body, headers=headers)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        urllib.request.urlopen(req, timeout=self._timeout, context=_ssl_context())


# --------------------------------------------------------------------------- env wiring
def sinks_from_env(env: Mapping[str, str]) -> list[PushSink]:
    """Build the configured phone sinks from environment variables. Returns [] if none set.

    NOTIFY_MIN_SEVERITY (info|warning|critical, default warning)
    Telegram:  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    ntfy:      NTFY_TOPIC [+ NTFY_SERVER (default https://ntfy.sh) + NTFY_TOKEN]
    """
    try:
        min_sev = Severity((env.get("NOTIFY_MIN_SEVERITY") or "warning").lower())
    except ValueError:
        min_sev = Severity.WARNING
    sinks: list[PushSink] = []
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        sinks.append(TelegramSink(tok, chat, min_severity=min_sev))
    topic = env.get("NTFY_TOPIC")
    if topic:
        sinks.append(NtfySink(topic, server=env.get("NTFY_SERVER") or "https://ntfy.sh",
                              min_severity=min_sev, token=env.get("NTFY_TOKEN")))
    return sinks
