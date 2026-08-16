"""Trader Consensus Engine (§ Phase G2.5) — PURE, deterministic.

For a symbol, aggregate the tracked traders' disclosed directions (LONG / SHORT / NEUTRAL) into
quality-WEIGHTED shares: a trader's influence is proportional to their quality score, so a low-quality
trader does not count the same as a high-quality one. Unknown quality gets a minimal floor weight (it
barely moves the consensus). No positions → all None (NO DATA); nothing is fabricated.
"""
from __future__ import annotations

from dataclasses import dataclass

_FLOOR = 1.0   # minimal weight for a trader with unknown quality (so unknowns barely influence)


@dataclass(slots=True)
class ConsensusResult:
    consensus: str | None       # BULLISH / BEARISH / NEUTRAL / None
    long_percent: float | None
    short_percent: float | None
    neutral_percent: float | None
    weighted_score: float | None   # mean contributor quality 0-100 = "how good are these traders"
    contributor_count: int


def _weight(q: float | None) -> float:
    return max(_FLOOR, q) if q is not None else _FLOOR


def compute_consensus(entries: list[tuple[str, float | None]]) -> ConsensusResult:
    """entries = [(direction, quality_score_or_None), …]. Quality-weighted directional shares."""
    valid = [((d or "").upper(), q) for (d, q) in entries if d]
    if not valid:
        return ConsensusResult(None, None, None, None, None, 0)
    total = sum(_weight(q) for _, q in valid)
    long_w = sum(_weight(q) for d, q in valid if d == "LONG")
    short_w = sum(_weight(q) for d, q in valid if d == "SHORT")
    neutral_w = sum(_weight(q) for d, q in valid if d == "NEUTRAL")
    lp = round(100.0 * long_w / total, 1)
    sp = round(100.0 * short_w / total, 1)
    npct = round(100.0 * neutral_w / total, 1)
    if long_w > short_w:
        label = "BULLISH"
    elif short_w > long_w:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    quals = [q for _, q in valid if q is not None]
    weighted_score = round(sum(quals) / len(quals), 1) if quals else None
    return ConsensusResult(label, lp, sp, npct, weighted_score, len(valid))
