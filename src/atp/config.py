"""System configuration (§15 Trading Policy, §24).

One place that captures the *single* set of decisions the desk runs on — capital mandate, risk
limits, which specialists are enabled, regime and execution settings — and projects them onto
the runtime objects (`TradingPolicy`, strategy list, execution knobs). Loadable from JSON so a
deployment is configured once and reproducibly (§15). Stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .policy import TradingPolicy
from .regime.classifier import RegimeClassifier


@dataclass(slots=True)
class ExecutionConfig:
    spread_bps: float = 2.0
    slippage_bps: float = 1.0
    commission_per_unit: float = 0.005
    min_commission: float = 1.0
    impact_eta_bps: float | None = None      # None => no market-impact model
    slices: int | None = None                # None => immediate; N => TWAP over N bars
    vwap_profile: list[float] | None = None  # set => VWAP weights (overrides slices count)


@dataclass(slots=True)
class RegimeConfig:
    trend_threshold: float = 0.3
    low_vol_percentile: float = 0.3
    high_vol_percentile: float = 0.85


@dataclass(slots=True)
class SystemConfig:
    # --- capital & risk (the Trading Policy, §15) --------------------------
    capital: float = 100_000.0
    risk_per_trade: float = 0.01
    daily_loss_limit: float = 0.03
    max_position_pct: float = 0.20
    max_leverage: float = 1.0
    max_open_positions: int = 10

    # --- which specialists to run (feature-only ones; engine-backed ones are
    #     wired programmatically — see app.build_strategies) -----------------
    strategies: list[str] = field(default_factory=lambda: ["momentum", "mean_reversion"])

    regime: RegimeConfig = field(default_factory=RegimeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # ---------------------------------------------------------------- API --
    def to_policy(self) -> TradingPolicy:
        return TradingPolicy(
            capital=self.capital,
            risk_per_trade=self.risk_per_trade,
            daily_loss_limit=self.daily_loss_limit,
            max_position_pct=self.max_position_pct,
            max_leverage=self.max_leverage,
            max_open_positions=self.max_open_positions,
        )

    def to_regime(self) -> RegimeClassifier:
        r = self.regime
        return RegimeClassifier(
            trend_threshold=r.trend_threshold,
            low_vol_percentile=r.low_vol_percentile,
            high_vol_percentile=r.high_vol_percentile,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "SystemConfig":
        data = dict(data)
        regime = data.pop("regime", None)
        execution = data.pop("execution", None)
        cfg = cls(**data)
        if regime:
            cfg.regime = RegimeConfig(**regime)
        if execution:
            cfg.execution = ExecutionConfig(**execution)
        return cfg

    @classmethod
    def from_json_file(cls, path: str) -> "SystemConfig":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
