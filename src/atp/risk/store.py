"""Persistence for the 3-parameter TRADING RISK config (§15).

The user's three settings (capital, risk-per-trade, max-daily-loss) must survive a restart of the
trading system. They are stored as a small JSON file — no secrets, just the three numbers — and
reloaded on startup. Atomic write (temp + rename) so a crash mid-write can't corrupt the file.
"""

from __future__ import annotations

import json
import os
import tempfile

from .config import TradingRiskConfig

DEFAULT_PATH = os.environ.get("ATP_RISK_CONFIG_PATH") or os.path.expanduser("~/.atp/risk_config.json")


class RiskConfigStore:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path

    def load(self) -> TradingRiskConfig | None:
        """Return the persisted config, or None if none exists / it is unreadable."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return TradingRiskConfig(
                capital=float(data["capital"]),
                risk_per_trade_pct=float(data["risk_per_trade_pct"]),
                max_daily_loss_pct=float(data["max_daily_loss_pct"]),
            )
        except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(self, config: TradingRiskConfig) -> None:
        """Atomically persist the three parameters."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {
            "capital": config.capital,
            "risk_per_trade_pct": config.risk_per_trade_pct,
            "max_daily_loss_pct": config.max_daily_loss_pct,
        }
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".risk_config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)   # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
