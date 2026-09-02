"""§ R3.1A.2 — deterministic store-URL resolution for the one-shot research workers (RESEARCH ONLY).

Production ships its database credentials in `/opt/atp/atp.env` as `ATP_DATABASE_URL` — or as the
`ATP_APP_USER` / `ATP_APP_PASSWORD` / `ATP_PROD_DB` components that `atp.services.base.build_dsn` assembles
for the supervised services. The one-shot workers originally accepted ONLY `ATP_STORE_URL` / `DATABASE_URL`,
so a correctly installed unit would still have exited 2 without ever writing a snapshot. This resolver
accepts the same production variables, in an explicit precedence order, and returns None (fail closed —
the worker exits non-zero) rather than guessing. It never logs, prints or returns the password separately;
`describe_source` names only the variable that supplied the URL, never its value.
"""
from __future__ import annotations

import os

#: Explicit precedence. The first variable that is set and non-empty wins.
URL_VARS = ("ATP_STORE_URL", "DATABASE_URL", "ATP_DATABASE_URL")
#: Component fallback — the same variables `atp.services.base.build_dsn` uses for the supervised services.
COMPONENT_VARS = ("ATP_APP_USER", "ATP_APP_PASSWORD", "ATP_PROD_DB", "ATP_PG_HOST", "ATP_PG_PORT")


def resolve_store_url(env: dict | None = None) -> str | None:
    """The worker's store URL, or None when production credentials are absent (never a guessed default)."""
    env = os.environ if env is None else env
    for var in URL_VARS:
        url = (env.get(var) or "").strip()
        if url:
            return url
    password = (env.get("ATP_APP_PASSWORD") or "").strip()
    if not password:
        return None                                   # no URL and no credentials → fail closed
    user = (env.get("ATP_APP_USER") or "atp_app").strip()
    db = (env.get("ATP_PROD_DB") or "atp_prod").strip()
    host = (env.get("ATP_PG_HOST") or "127.0.0.1").strip()
    port = (env.get("ATP_PG_PORT") or "5432").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def describe_source(env: dict | None = None) -> str:
    """Which variable supplied the URL — a NAME only, never a secret. 'NONE' when nothing is configured."""
    env = os.environ if env is None else env
    for var in URL_VARS:
        if (env.get(var) or "").strip():
            return var
    return "ATP_APP_* components" if (env.get("ATP_APP_PASSWORD") or "").strip() else "NONE"


def missing_config_reason() -> dict:
    """The fail-closed diagnostic a worker prints when no store URL can be resolved (no secrets)."""
    return {"ok": False, "error": "NO_STORE_URL",
            "reason": "no store url configured",
            "accepted": [*URL_VARS, "or " + "/".join(COMPONENT_VARS)]}
