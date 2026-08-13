"""PAPER AUTONOMOUS demo — runs the engine on synthetic data, ARMED, and prints the decisions.

This is a manual demo (you run it); it is NOT auto-started and NEVER touches IBKR. Execution is
the internal PaperBroker. It shows the mode/status, the data-quality gate, and the decision feed.

    PYTHONPATH=src python3 examples/paper_autonomous_demo.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from atp.autonomous import PaperAutonomousEngine
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.policy import TradingPolicy
from atp.strategy import BreakoutStrategy, MeanReversionStrategy, MomentumStrategy

INST = Instrument("DEMO", AssetClass.EQUITY)
START = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)


def bars(n=80):
    out, p = [], 100.0
    for i in range(n):
        p = 100.0 + 0.5 * i
        out.append(Bar(INST, p, p * 1.002, p * 0.998, p, 5000, START + timedelta(minutes=i)))
    return out


async def main():
    journal = InMemoryJournal()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()],
        journal=journal)
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, journal=journal)

    print(f"status before arming : {eng.status.value}  (default DISABLED — no trading)")
    eng.arm()
    print(f"status after  arming : {eng.status.value}  (PAPER, never live)")

    b = bars()
    md = [{"symbol": "DEMO", "status": "DATA_AVAILABLE", "market_data_type": "REALTIME",
           "bid": b[-1].close * 0.9999, "ask": b[-1].close * 1.0001}]
    await eng.step(now=b[-1].ts, bars=b, market_data=md)

    acct = await broker.get_account()
    snap = eng.snapshot(account=acct)
    print("\n── AUTONOMOUS TRADING ──────────────────────")
    print(f"  mode={snap['mode']} status={snap['status']} live_execution={snap['live_execution']} ibkr_orders={snap['ibkr_orders']}")
    print(f"  paper_equity={snap['paper_equity']:.2f}  trades_today={snap['trades_today']}  open_positions={snap['open_positions']}")
    print("\n── AI DECISION FEED ────────────────────────")
    for d in snap["decisions"]:
        print(f"  {d['ts'][11:19]} {d['instrument']:<8} {str(d['action']):<5} qty={d['quantity']} "
              f"px={d['price']} -> {d['decision']}  ({d['reason']})")

    # Data-quality gate demo: unavailable data -> NO TRADE
    await eng.step(now=b[-1].ts + timedelta(minutes=1), bars=b[-1:],
                   market_data=[{"symbol": "DEMO", "status": "DATA_NOT_AVAILABLE"}])
    print("\n  (fed unavailable data -> last decision:",
          eng.snapshot(account=await broker.get_account())["decisions"][0]["decision"], ")")


if __name__ == "__main__":
    asyncio.run(main())
