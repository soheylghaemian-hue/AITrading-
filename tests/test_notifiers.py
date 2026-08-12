"""Phone push notifications — ntfy / Telegram sinks + NotificationCenter forwarding.

Network is never touched: the request *builders* are pure and tested directly, and the
NotificationCenter is tested with a fake in-memory sink.
"""

from datetime import datetime, timezone

from atp.dashboard.notifications import Kind, NotificationCenter, Notification, Severity
from atp.dashboard.notifiers import (
    NtfySink, TelegramSink, format_message, ntfy_request, passes, sinks_from_env, telegram_request,
)


def _n(kind=Kind.RISK_HALT, sev=Severity.CRITICAL, msg="daily loss limit reached"):
    return Notification(datetime(2026, 8, 12, tzinfo=timezone.utc), sev, kind, msg)


# --------------------------------------------------------------------------- helpers
def test_severity_filter():
    assert passes(Severity.CRITICAL, Severity.WARNING)
    assert passes(Severity.WARNING, Severity.WARNING)
    assert not passes(Severity.INFO, Severity.WARNING)


def test_format_message_has_severity_and_kind():
    m = format_message(_n(msg="broker down"))
    assert "CRITICAL" in m and "risk_halt" in m and "broker down" in m


# --------------------------------------------------------------------------- request builders (no network)
def test_telegram_request_builds_url_and_body():
    url, body = telegram_request("TOK", "12345", _n(msg="hello"))
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    text = body.decode()
    assert "chat_id=12345" in text and "hello" in text


def test_ntfy_request_builds_url_headers_body():
    url, body, headers = ntfy_request("https://ntfy.sh", "my-secret-topic", _n(msg="halt!"))
    assert url == "https://ntfy.sh/my-secret-topic"
    assert body == b"halt!"
    assert headers["Priority"] == "urgent"          # critical → urgent
    assert headers["Tags"] == "critical"


# --------------------------------------------------------------------------- env wiring
def test_sinks_from_env_none():
    assert sinks_from_env({}) == []


def test_sinks_from_env_telegram_and_ntfy():
    sinks = sinks_from_env({
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
        "NTFY_TOPIC": "topic", "NOTIFY_MIN_SEVERITY": "critical",
    })
    kinds = {type(s).__name__ for s in sinks}
    assert kinds == {"TelegramSink", "NtfySink"}


def test_sinks_from_env_partial_telegram_ignored():
    # token without chat id → no telegram sink
    assert sinks_from_env({"TELEGRAM_BOT_TOKEN": "t"}) == []


# --------------------------------------------------------------------------- forwarding
class _FakeSink:
    def __init__(self, *, boom=False):
        self.delivered = []
        self._boom = boom
    def deliver(self, n):
        if self._boom:
            raise RuntimeError("network down")
        self.delivered.append(n)


def test_center_forwards_to_sink():
    sink = _FakeSink()
    nc = NotificationCenter(sinks=[sink])
    nc.push(Kind.EMERGENCY_STOP, "stopped", severity=Severity.CRITICAL)
    assert len(sink.delivered) == 1
    assert sink.delivered[0].kind is Kind.EMERGENCY_STOP


def test_push_survives_sink_failure():
    boom, good = _FakeSink(boom=True), _FakeSink()
    nc = NotificationCenter(sinks=[boom, good])
    n = nc.push(Kind.RISK_HALT, "halt", severity=Severity.CRITICAL)  # must not raise
    assert n.kind is Kind.RISK_HALT
    assert len(nc) == 1                 # still recorded locally
    assert len(good.delivered) == 1     # other sinks still fire


def test_from_env_builds_center_with_sinks():
    nc = NotificationCenter.from_env(env={"NTFY_TOPIC": "abc", "NOTIFY_MIN_SEVERITY": "info"})
    # an INFO push is below no threshold here; the ntfy sink would try network, so use a center
    # with no sinks path instead: just assert the sink was constructed.
    assert len(nc._sinks) == 1  # noqa: SLF001 — test introspection
