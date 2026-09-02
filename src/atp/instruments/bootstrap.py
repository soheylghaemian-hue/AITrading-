"""WP9 — the global-instrument-universe bootstrap wiring (fail-closed CLI).

Wires the durable store + the fail-closed source registry + the official-directory adapters into the existing
WP2 importer and WP3 IBKR qualifier, so the persistent catalogue can be filled from real reference data. It
does NOT enable any source by itself: every declared source is ``available=False`` until an operator attaches
a real, entitled provider and explicitly activates it. `run_import` refuses to import from a source that is
not usable (MISSING/BLOCKED), so a plain run performs NO import and NO network access — the machinery exists,
activation is a separate, authorized step.

  bootstrap sources                 → list the declared sources with availability + license (fail-closed)
  bootstrap coverage --db URL       → the read-only coverage read-model
  bootstrap import   --db URL …     → refused unless the named source is activated + usable (fail-closed)

SAFETY: reference data only. Read-only IBKR qualification (reqContractDetails) only. No orders, no execution,
no market-data subscription, no paid data purchase, no provider auto-activation.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from ..store import open_store
from .coverage import instrument_coverage
from .importer import MarketSource, import_instruments
from .sources import InstrumentSourceEntry, seed_sources, source_by_id


class FailClosedError(RuntimeError):
    """Raised when an operation is refused because a source is not activated / not usable."""


def resolve_source(source_id: str) -> InstrumentSourceEntry:
    entry = source_by_id(source_id)
    if entry is None:
        raise FailClosedError(f"unknown source '{source_id}'")
    return entry


def activate(source: InstrumentSourceEntry, **overrides) -> InstrumentSourceEntry:
    """Produce an explicitly-activated copy of a declared source. This is the ONLY way a source becomes
    usable, and it is a deliberate, out-of-band operator action (never automatic) — used by tests and by an
    operator who has confirmed the entitlement/license. It never bypasses a BLOCKED license by itself: the
    caller must also clear `blocked_reason` and set a permissive `license_status`/`storage_allowed`."""
    return dataclasses.replace(source, **overrides)


def ensure_importable(source: InstrumentSourceEntry) -> None:
    """Fail-closed gate: refuse to import from a source that is not available/usable (license unknown or
    blocked, or storage not permitted). This is what keeps a default run from touching any real source."""
    if not source.available:
        raise FailClosedError(
            f"source '{source.source_id}' is not activated (available=False) — refusing import (fail-closed)")
    if source.blocked_reason:
        raise FailClosedError(
            f"source '{source.source_id}' is BLOCKED: {source.blocked_reason} — refusing import (fail-closed)")
    if not source.usable:
        raise FailClosedError(
            f"source '{source.source_id}' license does not permit use (license_status="
            f"{source.license_status}, storage_allowed={source.storage_allowed}) — refusing (fail-closed)")


def run_import(store, *, source: InstrumentSourceEntry, markets: list[MarketSource],
               source_label: str | None = None):
    """Import a source's markets into the durable catalogue — ONLY if the source is activated + usable.
    The heavy lifting (idempotency, resume, dedup, provenance, audit events, per-market isolation) is the
    existing WP2 importer; this function only adds the fail-closed source gate."""
    ensure_importable(source)
    return import_instruments(store, source_label=source_label or source.source_id, markets=markets)


def render_sources() -> list[dict]:
    return [s.summary() for s in seed_sources()]


def render_coverage(store) -> dict:
    return instrument_coverage(store)


# --------------------------------------------------------------------------- CLI
def _cmd_sources(_args) -> int:
    print(json.dumps({"sources": render_sources()}, indent=2, sort_keys=True))
    return 0


def _cmd_coverage(args) -> int:
    store = open_store(args.db)
    try:
        print(json.dumps(render_coverage(store), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def _cmd_import(args) -> int:
    # Fail-closed by design: the declared sources are all available=False, so this refuses and explains why.
    # Activating a source (attaching an entitled provider) is a separate, authorized operator step.
    try:
        source = resolve_source(args.source)
        ensure_importable(source)
    except FailClosedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print("REFUSED: no directory provider is attached in this build — import is a separate authorized step.",
          file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atp.instruments.bootstrap",
                                     description="Global instrument universe bootstrap (fail-closed).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="list declared directory sources (availability + license)")
    p_cov = sub.add_parser("coverage", help="print the read-only coverage read-model")
    p_cov.add_argument("--db", required=True, help="store URL (sqlite path or postgres:// DSN)")
    p_imp = sub.add_parser("import", help="import from a source (refused unless activated + usable)")
    p_imp.add_argument("--db", required=True)
    p_imp.add_argument("--source", required=True, help="declared source id (see `sources`)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"sources": _cmd_sources, "coverage": _cmd_coverage, "import": _cmd_import}[args.command](args)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
