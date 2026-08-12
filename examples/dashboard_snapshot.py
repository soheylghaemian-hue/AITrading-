"""Generate a real dashboard snapshot from a paper run and write it next to the static page.

    PYTHONPATH=src python3 examples/dashboard_snapshot.py

Runs a short paper session (same desk as everywhere), then serializes `build_snapshot(...)`
to src/atp/dashboard/static/snapshot.json. The bundled static/index.html falls back to that
file, so you can view the dashboard with real data without the FastAPI server:

    python3 -m http.server 8000 --directory src/atp/dashboard/static
    # open http://localhost:8000/

For the live server instead: `pip install -e ".[live]"`, then serve `create_app(context)`
with uvicorn (see atp.dashboard.api).
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard.snapshot import build_snapshot
from atp.governance import DecayPolicy, GovernanceMonitor, StrategyRegistry
from atp.journal import InMemoryJournal
from atp.live import LiveRunner, ReplayFeed, build_paper_stack
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy import MeanReversionStrategy, MomentumStrategy

INST = Instrument("ACME", AssetClass.EQUITY)
START = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday
OUT = Path(__file__).resolve().parents[1] / "src/atp/dashboard/static/snapshot.json"


def bars(n=480):
    out = []
    for i in range(n):
        p = 100 + 0.05 * i + 5 * math.sin(i / 12.0) + 1.2 * math.sin(i / 2.7)
        out.append(Bar(INST, p, p * 1.002, p * 0.998, p, 1000 + (i % 40) * 25,
                       START + timedelta(minutes=i)))
    return out


async def main():
    journal = InMemoryJournal()
    registry = StrategyRegistry()
    registry.register("momentum")
    registry.register("mean_reversion")
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MomentumStrategy(), MeanReversionStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal, registry=registry,
    )
    await LiveRunner(
        desk=desk, broker=broker, feed=ReplayFeed(bars()),
        monitor=GovernanceMonitor(registry, DecayPolicy(min_trades=8)),
        journal=journal, govern_every=120,
    ).run()

    account = await broker.get_account()
    snap = build_snapshot(account=account, risk=risk, journal=journal,
                          registry=registry, market=desk.latest_market()).as_dict()
    OUT.write_text(json.dumps(snap, indent=2))
    print(f"wrote {OUT}")
    print(f"  equity={snap['account']['equity']:,.2f}  trades={snap['n_trades']}  "
          f"positions={len(snap['positions'])}")


if __name__ == "__main__":
    asyncio.run(main())
