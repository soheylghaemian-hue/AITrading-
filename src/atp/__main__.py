"""atp command-line entrypoint (§24).

    python -m atp version
    python -m atp config [--config path]      # print the (default or loaded) config as JSON
    python -m atp backtest [--config path] [--bars N] [--instrument SYM]

`backtest` assembles the desk from a `SystemConfig`, replays a synthetic dataset, and prints the
performance metrics and the §11 per-strategy edge — the whole pipeline as one command.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from datetime import datetime, timedelta, timezone

from . import __version__
from .app import run_backtest
from .config import SystemConfig
from .core.enums import AssetClass
from .core.events import Bar, Instrument
from .journal.analytics import TradeAnalytics


def synthetic_bars(symbol: str, n: int = 500) -> list[Bar]:
    inst = Instrument(symbol, AssetClass.EQUITY)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday
    bars = []
    for i in range(n):
        p = 100.0 + 0.05 * i + 5.0 * math.sin(i / 12.0) + 1.2 * math.sin(i / 2.7)
        bars.append(Bar(inst, p, p * 1.002, p * 0.998, p, 1000 + (i % 40) * 25,
                        start + timedelta(minutes=i)))
    return bars


def _load_config(path: str | None) -> SystemConfig:
    return SystemConfig.from_json_file(path) if path else SystemConfig()


def _cmd_config(args) -> int:
    print(_load_config(args.config).to_json())
    return 0


def _cmd_version(_args) -> int:
    print(f"atp {__version__}")
    return 0


def _cmd_backtest(args) -> int:
    config = _load_config(args.config)
    bars = synthetic_bars(args.instrument, args.bars)
    run = asyncio.run(run_backtest(config, bars, periods_per_year=252 * 390))
    m = run.result.metrics(periods_per_year=252 * 390)

    print("=" * 60)
    print(f"  atp backtest — {args.instrument}, {args.bars} bars")
    print(f"  strategies: {', '.join(config.strategies)}")
    print("=" * 60)
    print(f"  executed / blocked : {run.result.n_executed} / {run.result.n_blocked}")
    print(f"  start / end equity : {run.result.starting_equity:,.0f} -> {run.result.ending_equity:,.2f}")
    print(f"  total return       : {m.total_return:+.2%}")
    print(f"  max drawdown       : {m.max_drawdown:.2%}")
    print(f"  trades / win rate  : {m.n_trades} / {m.win_rate:.0%}")
    print(f"  profit factor      : {m.profit_factor:.2f}")
    an = TradeAnalytics.from_journal(run.journal)
    if an.overall().n_trades:
        print("-" * 60)
        print("  edge by strategy (§11):")
        for g in an.by_strategy():
            print(f"    {g.label:<16} n={g.n_trades:>3}  win={g.win_rate:>4.0%}  "
                  f"expectancy={g.expectancy:>+8.2f}")
    print("=" * 60)
    print("  Synthetic data — machinery demo, not a performance claim.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atp", description="Autonomous Multi-Asset Trading desk")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("version").set_defaults(func=_cmd_version)

    pc = sub.add_parser("config", help="print the config as JSON")
    pc.add_argument("--config", default=None, help="path to a config JSON file")
    pc.set_defaults(func=_cmd_config)

    pb = sub.add_parser("backtest", help="run a synthetic backtest")
    pb.add_argument("--config", default=None, help="path to a config JSON file")
    pb.add_argument("--bars", type=int, default=500)
    pb.add_argument("--instrument", default="DEMO")
    pb.set_defaults(func=_cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
