"""Idempotent order lifecycle (§ Phase B) — PaperBroker first, no live broker yet.

Flow:  CREATE INTENT → persist → risk authorization → submit ONCE → persist broker ack + fill +
position (one transaction) → FILLED.

An order must NEVER be submitted twice because a process restarted:
  * every order carries a caller-supplied `idempotency_key`; a second `place()` with the same key
    returns the existing order instead of creating/submitting a new one;
  * the broker submit itself is keyed by `client_order_id`, so even a retry after a crash between
    "broker executed" and "DB committed" fills exactly once.
"""

from __future__ import annotations

from collections.abc import Callable

from ..store.base import FillRow, OrderRow, new_id, utcnow_iso
from ..store.money import D
from .positions import apply_fill_to_position

TERMINAL = {"FILLED", "REJECTED", "CANCELLED"}


class OrderManager:
    def __init__(self, store):
        self._store = store

    def place(self, *, idempotency_key: str, instrument: str, side: str, quantity,
              order_type: str = "MARKET", correlation_id: str,
              authorize: Callable[[], tuple[bool, str]],
              fill: Callable[[str], dict]) -> OrderRow:
        """`authorize()` → (approved, reason) is the authoritative Risk decision.
        `fill(client_order_id)` submits to the (paper) broker idempotently and returns
        {broker_order_id, price, commission, filled_qty}. AI can never bypass `authorize`."""
        existing = self._store.get_order_by_idempotency(idempotency_key)
        if existing is not None:
            # Already handled (or in-flight) — do NOT create or submit a duplicate.
            if existing.state in TERMINAL or existing.state == "SUBMITTED":
                return existing
            coid = existing.client_order_id
        else:
            coid = "co_" + new_id()
            self._store.insert_order_intent(
                client_order_id=coid, idempotency_key=idempotency_key, instrument=instrument,
                side=side, quantity=D(quantity), order_type=order_type, correlation_id=correlation_id)

        order = self._store.get_order(coid)

        # 1) Risk authorization (authoritative veto).
        if order.state == "INTENT":
            approved, reason = authorize()
            if not approved:
                self._store.update_order_state(client_order_id=coid, state="REJECTED", reason=reason)
                return self._store.get_order(coid)
            self._store.update_order_state(client_order_id=coid, state="AUTHORIZED", reason="risk approved")
            order = self._store.get_order(coid)

        # 2) Submit ONCE. Broker submit is idempotent by client_order_id.
        if order.state == "AUTHORIZED":
            ack = fill(coid)
            fillrow = FillRow(
                fill_id="fl_" + new_id(), client_order_id=coid, instrument=instrument, side=side,
                quantity=D(ack.get("filled_qty", quantity)), price=D(ack["price"]),
                commission=D(ack.get("commission", 0)), ts=utcnow_iso())
            new_pos = apply_fill_to_position(self._store.get_position(instrument), fillrow)
            # ack + fill + position + FILLED committed atomically
            self._store.apply_fill(fill=fillrow, position=new_pos, order_state="FILLED",
                                   broker_order_id=ack["broker_order_id"])

        return self._store.get_order(coid)
