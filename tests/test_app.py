"""Application/config tests (§15/§24): config round-trip, strategy assembly, config-driven
backtest, and the CLI."""

import io
import math
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.app import build_strategies, make_backtester, run_backtest
from atp.config import ExecutionConfig, RegimeConfig, SystemConfig
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument

INST = Instrument("DEMO", AssetClass.EQUITY)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _bars(n=150):
    return [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                1000, T0 + timedelta(minutes=i)) for i in range(n)]


# --------------------------------------------------------------------------- config
def test_config_defaults_and_policy_projection():
    cfg = SystemConfig()
    pol = cfg.to_policy()
    assert pol.capital == 100_000.0
    assert pol.risk_per_trade == 0.01
    assert cfg.strategies == ["momentum", "mean_reversion"]


def test_config_json_roundtrip():
    cfg = SystemConfig(capital=250_000.0, strategies=["momentum", "breakout"],
                       regime=RegimeConfig(trend_threshold=0.25),
                       execution=ExecutionConfig(slices=4, impact_eta_bps=50.0))
    restored = SystemConfig.from_dict(cfg.to_dict())
    assert restored.capital == 250_000.0
    assert restored.strategies == ["momentum", "breakout"]
    assert restored.regime.trend_threshold == 0.25
    assert restored.execution.slices == 4
    assert restored.execution.impact_eta_bps == 50.0


def test_config_from_json_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cfg.json"
        p.write_text(SystemConfig(capital=500_000.0).to_json())
        cfg = SystemConfig.from_json_file(str(p))
    assert cfg.capital == 500_000.0


# --------------------------------------------------------------------------- strategy assembly
def test_build_strategies_by_name():
    strats = build_strategies(SystemConfig(strategies=["momentum", "mean_reversion", "breakout"]))
    assert [s.name for s in strats] == ["momentum", "mean_reversion", "breakout"]


def test_build_strategies_rejects_engine_backed():
    with pytest.raises(ValueError, match="engine-backed"):
        build_strategies(SystemConfig(strategies=["stat_arb"]))


def test_build_strategies_rejects_unknown():
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategies(SystemConfig(strategies=["nope"]))


# --------------------------------------------------------------------------- run
async def test_run_backtest_from_config_produces_result_and_journal():
    cfg = SystemConfig(strategies=["momentum"],
                       regime=RegimeConfig(trend_threshold=0.25, low_vol_percentile=0.4))
    run = await run_backtest(cfg, _bars(200))
    assert run.result.n_executed > 0
    assert len(run.journal) > 0
    m = run.result.metrics()
    assert 0.0 <= m.max_drawdown <= 1.0


async def test_make_backtester_wires_execution_config():
    cfg = SystemConfig(strategies=["momentum"],
                       regime=RegimeConfig(trend_threshold=0.25, low_vol_percentile=0.4),
                       execution=ExecutionConfig(slices=4, impact_eta_bps=100.0))
    bt = make_backtester(cfg)
    res = await bt.run(_bars(200))
    assert res.n_executed > 0        # runs with impact model + TWAP slicing configured


# --------------------------------------------------------------------------- CLI
def _run_cli(argv) -> tuple[int, str]:
    from atp.__main__ import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def test_cli_version():
    code, out = _run_cli(["version"])
    assert code == 0 and "atp" in out


def test_cli_config_prints_json():
    code, out = _run_cli(["config"])
    assert code == 0 and '"capital"' in out and '"strategies"' in out


def test_cli_backtest_runs():
    code, out = _run_cli(["backtest", "--bars", "120"])
    assert code == 0 and "atp backtest" in out and "total return" in out
