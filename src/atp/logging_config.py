"""Structured logging + audit trail (§21 Logging).

A single place to obtain loggers so every component logs under the `atp.*` namespace with a
consistent format. Real deployments swap the handler for JSON/structured output shipped to
the monitoring stack (§21); the call sites don't change.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("ATP_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger("atp")
    root.setLevel(getattr(logging, level, logging.INFO))
    # Avoid duplicate handlers if reconfigured (e.g. in test reruns).
    if not root.handlers:
        root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. get_logger('risk') -> 'atp.risk'."""
    _configure_root()
    return logging.getLogger(f"atp.{name}")
