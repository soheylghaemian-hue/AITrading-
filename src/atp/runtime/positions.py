"""Position math + reconstruction + reconciliation (§ Phase B).

Positions are derived from durable fills (weighted average, realized P&L on reduction) and MUST be
reconciled against broker state before trading resumes. Neither the database position alone nor the
broker position alone is sufficient — a mismatch forces HALT / RECOVERY_REQUIRED.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..store.base import FillRow, PositionRow, utcnow_iso
from ..store.money import D


def apply_fill_to_position(pos: PositionRow | None, fill: FillRow) -> PositionRow:
    """Pure: fold a fill into a position. Weighted-average cost; realized P&L booked on reduction;
    commission always reduces realized P&L. Uses exact Decimal throughout."""
    cur_qty = pos.quantity if pos else D(0)
    cur_avg = pos.avg_price if pos else D(0)
    realized = (pos.realized_pnl if pos else D(0)) - D(fill.commission)
    signed = D(fill.quantity) if fill.side.upper() == "BUY" else -D(fill.quantity)
    new_qty = cur_qty + signed

    same_dir = cur_qty == 0 or (cur_qty > 0) == (signed > 0)
    if same_dir:
        total = cur_avg * abs(cur_qty) + D(fill.price) * abs(signed)
        new_avg = (total / abs(new_qty)) if new_qty != 0 else D(0)
    else:
        closed = min(abs(signed), abs(cur_qty))
        direction = D(1) if cur_qty > 0 else D(-1)
        realized += (D(fill.price) - cur_avg) * closed * direction
        new_avg = cur_avg if abs(signed) <= abs(cur_qty) else D(fill.price)
        if new_qty == 0:
            new_avg = D(0)
    return PositionRow(fill.instrument, new_qty, new_avg, realized, utcnow_iso())


def reconstruct_positions(store) -> dict[str, PositionRow]:
    """Replay all durable fills into positions (the authoritative reconstruction after a restart)."""
    out: dict[str, PositionRow] = {}
    for f in store.list_fills():
        out[f.instrument] = apply_fill_to_position(out.get(f.instrument), f)
    return out


@dataclass(slots=True)
class ReconResult:
    ok: bool
    diffs: list[tuple[str, Decimal, Decimal]]   # (instrument, db_qty, broker_qty)


def reconcile(db_positions: dict[str, Decimal], broker_positions: dict[str, Decimal],
              *, tol: Decimal = D("0.00000001")) -> ReconResult:
    """Compare DB-derived positions against broker-reported positions. Any mismatch → not ok."""
    diffs: list[tuple[str, Decimal, Decimal]] = []
    for inst in sorted(set(db_positions) | set(broker_positions)):
        dq = D(db_positions.get(inst, 0))
        bq = D(broker_positions.get(inst, 0))
        if abs(dq - bq) > tol:
            diffs.append((inst, dq, bq))
    return ReconResult(len(diffs) == 0, diffs)
