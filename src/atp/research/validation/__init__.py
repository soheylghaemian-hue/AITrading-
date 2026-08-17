"""§ R3.1A — deterministic AI-prediction-quality validation over immutable point-in-time snapshots/outcomes.

RESEARCH ONLY, kept strictly separate from trading P&L. Reports INSUFFICIENT until the preregistered
evidence gate passes; never fabricates a metric; heuristic confidence is never treated as a probability.
Imports nothing from execution / broker / IBKR / autonomous / F2 / kill-switch.
"""
from __future__ import annotations

from . import benchmarks, calibration, metrics, readmodel
from .runner import run_validation

__all__ = ["run_validation", "metrics", "benchmarks", "calibration", "readmodel"]
