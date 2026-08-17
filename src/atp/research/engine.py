"""§ R3.0 — deterministic replay engine: membership coverage + phased Decimal fills/accounting.

Pure, seedless-deterministic (same bars + config ⇒ identical result). Internal-only fills: a "trade"
is a research record — NO broker/order/execution object is ever created.

CTO hotfix corrections:
  1. GAP-SAFE sizing — quantity is derived at the ACTUAL fill from `actual_risk_per_share = fill −
     persisted_stop` (the stop is never widened/recomputed), so a next-bar gap-up can never exceed the
     configured risk budget. Both expected and actual risk-per-share are persisted.
  2. TRUE event-time replay — a shared availability timestamp is processed in explicit phases
     (DAY_BOUNDARY → BAR_OPEN → INTRABAR_STOP → BAR_CLOSE → STRATEGY_DECISION → PORTFOLIO_SNAPSHOT).
     ALL opens use marks as of BEFORE this timestamp's closes, so one symbol's current close can never
     leak into another symbol's earlier open.
  3. DAILY state resets FIRST — the day/session rollover runs before any fill or risk gate.
  4/5. Coverage validates timestamp-set MEMBERSHIP (not just counts); fills carry the real logical fill
     time (1D → session open, not UTC midnight; 1h → bucket open); decision/fill/exit times stay
     semantically distinct and chronologically valid.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal

from . import calendars as cal
from .risk_adapter import evaluate_sim_risk
from .strategy import ENTER_LONG, PitContext, ResearchBar, ResearchStrategy

_Z = Decimal(0)
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
ENTER, EXIT = "ENTER", "EXIT"       # internal pending-fill kinds (distinct from strategy actions)


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENT)


def _floor_int(v: Decimal) -> Decimal:
    return v.to_integral_value(rounding=ROUND_DOWN)


# --------------------------------------------------------------------------- coverage preflight
@dataclass(slots=True)
class SymbolCoverage:
    symbol: str
    requested_start: str
    requested_end: str
    first_available_ts: str | None
    last_available_ts: str | None
    expected_bars: int
    available_bars: int          # IN-SESSION bars only (extra bars never compensate)
    missing_bars: int
    missing_ratio: float
    warmup_bars: int
    usable_bars: int
    duplicate_bars: int
    out_of_order_bars: int
    out_of_session_bars: int
    missing_timestamps: list[str]
    ok: bool
    reason: str | None


@dataclass(slots=True)
class CoverageReport:
    symbols: list[SymbolCoverage]
    ok: bool
    failure_code: str | None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "failure_code": self.failure_code,
                "symbols": [asdict(s) for s in self.symbols]}


def coverage_preflight(bars_by_symbol: dict[str, list[ResearchBar]], symbols: list[str],
                       policy: cal.AvailabilityPolicy, start_dt, end_dt, warmup: int,
                       *, min_bars: int = 60, min_coverage: Decimal = Decimal("0.95")) -> CoverageReport:
    """Membership-based coverage: every available bar's timestamp must belong to the expected session
    bucket set; out-of-session bars are flagged and never count toward coverage; missing expected
    timestamps are reported explicitly."""
    expected_set = cal.expected_bar_timestamps(start_dt, end_dt, policy)
    expected = len(expected_set)
    rows: list[SymbolCoverage] = []
    for sym in symbols:
        bars = bars_by_symbol.get(sym, [])
        seen: set[str] = set()
        present: set[str] = set()
        dup = out_of_session = 0
        ooo = sum(1 for i in range(1, len(bars)) if cal.parse_ts(bars[i].ts) < cal.parse_ts(bars[i - 1].ts))
        for b in bars:
            key = cal.norm_ts(b.ts)
            if key in seen:
                dup += 1
            seen.add(key)
            if key in expected_set:
                present.add(key)
            else:
                out_of_session += 1
        in_session = len(present)
        missing_set = expected_set - present
        missing = len(missing_set)
        coverage = (Decimal(in_session) / Decimal(expected)) if expected > 0 else _Z
        ratio = (missing / expected) if expected > 0 else 1.0
        usable = max(0, in_session - warmup)
        reason = None
        if in_session < min_bars:
            reason = f"only {in_session} in-session bars (< {min_bars} required)"
        elif expected == 0:
            reason = "no expected session bars in range"
        elif coverage < min_coverage:
            reason = f"coverage {coverage:.3f} < {min_coverage}"
        elif usable <= 0:
            reason = f"no usable decision bars after {warmup}-bar warm-up"
        elif dup > 0:
            reason = f"{dup} duplicate bars"
        elif ooo > 0:
            reason = f"{ooo} out-of-order bars"
        elif out_of_session > 0:
            reason = f"{out_of_session} unexpected out-of-session bars"
        rows.append(SymbolCoverage(
            symbol=sym, requested_start=start_dt.isoformat(), requested_end=end_dt.isoformat(),
            first_available_ts=(bars[0].ts if bars else None), last_available_ts=(bars[-1].ts if bars else None),
            expected_bars=expected, available_bars=in_session, missing_bars=missing, missing_ratio=round(ratio, 4),
            warmup_bars=warmup, usable_bars=usable, duplicate_bars=dup, out_of_order_bars=ooo,
            out_of_session_bars=out_of_session, missing_timestamps=sorted(missing_set)[:20],
            ok=(reason is None), reason=reason))
    ok = all(r.ok for r in rows) and len(rows) > 0
    return CoverageReport(symbols=rows, ok=ok,
                          failure_code=None if ok else "INSUFFICIENT_HISTORICAL_COVERAGE")


# --------------------------------------------------------------------------- portfolio + replay
@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_fill: Decimal
    initial_stop: Decimal
    entry_ts: str                 # decision-availability time (when the entry was decided)
    entry_fill_ts: str            # fill bar OPEN time (session open for 1D)
    entry_decision_id: str
    entry_commission: Decimal
    entry_slippage: Decimal
    expected_rps: Decimal         # decision-derived risk/share (= atr_mult·ATR)
    actual_rps: Decimal           # gap-safe risk/share at the real fill (= fill − persisted stop)
    bars_held: int = 0


@dataclass(slots=True)
class ReplayResult:
    decisions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity_points: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    starting_capital: Decimal = _Z
    ending_equity: Decimal = _Z


@dataclass(slots=True)
class Costs:
    spread_bps: Decimal
    slippage_bps: Decimal
    commission_per_share: Decimal
    min_commission: Decimal

    @property
    def adverse_bps(self) -> Decimal:
        return self.spread_bps / 2 + self.slippage_bps

    def buy_fill(self, ref: Decimal) -> Decimal:
        return ref * (Decimal(1) + self.adverse_bps / Decimal(10000))

    def sell_fill(self, ref: Decimal) -> Decimal:
        return ref * (Decimal(1) - self.adverse_bps / Decimal(10000))

    def commission(self, qty: Decimal) -> Decimal:
        return max(self.min_commission, self.commission_per_share * qty)

    def exec_slippage(self, ref: Decimal, qty: Decimal) -> Decimal:
        return qty * ref * (self.adverse_bps / Decimal(10000))


def replay(*, symbols: list[str], bars_by_symbol: dict[str, list[ResearchBar]],
           policy: cal.AvailabilityPolicy, strategy: ResearchStrategy, risk_config: dict, costs: Costs,
           starting_capital: Decimal, max_concurrent: int) -> ReplayResult:
    """Chronological, multi-symbol, long-only replay processed in explicit event phases per shared
    availability timestamp (see module docstring). `risk_config` carries Decimal capital + pct limits."""
    res = ReplayResult(starting_capital=starting_capital)
    cash = starting_capital
    realized = _Z
    peak_equity = starting_capital
    positions: dict[str, Position] = {}
    pending: dict[str, dict] = {}
    marks: dict[str, Decimal] = {}
    seq = {"dec": 0, "trade": 0, "equity": 0, "event": 0}
    risk_pct = Decimal(str(risk_config["max_position_risk_pct"]))
    max_expo_pct = Decimal(str(risk_config["max_portfolio_exposure_pct"]))
    cur_day: str | None = None
    day_start_equity = starting_capital
    realized_day_start = _Z

    def gross_exposure() -> Decimal:
        return sum((p.quantity * marks.get(s, p.entry_fill) for s, p in positions.items()), _Z)

    def unrealized() -> Decimal:
        return sum((p.quantity * (marks.get(s, p.entry_fill) - p.entry_fill) for s, p in positions.items()), _Z)

    def equity() -> Decimal:
        return cash + gross_exposure()

    def add_event(etype, *, ts=None, symbol=None, severity="INFO", **details):
        seq["event"] += 1
        res.events.append({"seq": seq["event"], "ts": ts, "event_type": etype, "severity": severity,
                           "symbol": symbol, "details": {k: (str(v) if isinstance(v, Decimal) else v)
                                                         for k, v in details.items()}})

    def do_exit(sym, ref_price, reason, exit_ts, exit_fill_ts, exit_decision_id, ambiguous=False):
        nonlocal cash, realized
        pos = positions.pop(sym)
        fill = costs.sell_fill(ref_price)
        comm = costs.commission(pos.quantity)
        slip = costs.exec_slippage(ref_price, pos.quantity)
        cash += pos.quantity * fill - comm
        gross = (fill - pos.entry_fill) * pos.quantity
        total_comm = pos.entry_commission + comm
        total_slip = pos.entry_slippage + slip
        net = gross - total_comm
        realized += net
        ret = (net / (pos.entry_fill * pos.quantity)) if pos.entry_fill * pos.quantity != 0 else _Z
        seq["trade"] += 1
        res.trades.append({
            "id": f"t{seq['trade']}", "symbol": sym, "entry_decision_id": pos.entry_decision_id,
            "exit_decision_id": exit_decision_id, "entry_ts": pos.entry_ts, "entry_fill_ts": pos.entry_fill_ts,
            "entry_price": _q(pos.entry_fill), "initial_stop_price": _q(pos.initial_stop), "exit_ts": exit_ts,
            "exit_fill_ts": exit_fill_ts, "exit_price": _q(fill), "quantity": pos.quantity,
            "gross_pnl": _q(gross), "commission": _q(total_comm), "slippage": _q(total_slip),
            "net_pnl": _q(net), "return_pct": round(float(ret * _HUNDRED), 4), "bars_held": pos.bars_held,
            "exit_reason": reason, "ambiguous": ambiguous,
            "expected_risk_per_share": _q(pos.expected_rps), "actual_risk_per_share": _q(pos.actual_rps)})

    def try_enter(sym, fill_bar, decision_avail, decision_id, evidence):
        nonlocal cash
        eq = equity()                                   # pre-open equity (marks = prior closes)
        rs = evaluate_sim_risk(
            risk_config, realized=(realized - realized_day_start), unrealized=unrealized(),
            ts=decision_avail, peak_equity=peak_equity, equity=eq,
            gross_pct=(gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z,
            net_pct=(gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z)
        ot = cal.bar_open_utc(fill_bar.ts, policy).isoformat()
        if rs["status"] == "BLOCKED":
            add_event("RISK_BLOCKED", ts=ot, symbol=sym, severity="WARNING", reasons=rs["reasons"]); return
        if rs["status"] == "WARNING":
            add_event("RISK_WARNING", ts=ot, symbol=sym, reasons=rs["reasons"])
        if len(positions) >= max_concurrent:
            add_event("MAX_CONCURRENT_REACHED", ts=ot, symbol=sym, severity="WARNING",
                      max_concurrent=max_concurrent); return
        initial_stop = Decimal(str(evidence["initial_stop"]))
        expected_rps = Decimal(str(evidence["risk_per_share"]))
        entry_fill = costs.buy_fill(fill_bar.open)
        actual_rps = entry_fill - initial_stop          # gap-safe: real fill vs the PERSISTED stop
        if actual_rps <= 0:
            add_event("INVALID_STOP_DISTANCE", ts=ot, symbol=sym, severity="WARNING",
                      expected_rps=expected_rps, actual_rps=actual_rps); return
        risk_budget = eq * risk_pct / _HUNDRED
        risk_qty = _floor_int(risk_budget / actual_rps)
        headroom = eq * max_expo_pct / _HUNDRED - gross_exposure()
        qty_expo = _floor_int(headroom / entry_fill) if entry_fill > 0 else _Z
        qty_cash = _floor_int((cash - costs.min_commission) / (entry_fill + costs.commission_per_share)) \
            if entry_fill > 0 else _Z
        qty = min(risk_qty, qty_expo, qty_cash)
        if qty <= 0:
            binding = "EXPOSURE_REJECTED" if qty_expo <= 0 else "INSUFFICIENT_CASH"
            add_event(binding, ts=ot, symbol=sym, severity="WARNING",
                      risk_qty=risk_qty, qty_expo=qty_expo, qty_cash=qty_cash); return
        comm = costs.commission(qty)
        slip = costs.exec_slippage(fill_bar.open, qty)
        cost = qty * entry_fill + comm
        if cost > cash:
            add_event("INSUFFICIENT_CASH", ts=ot, symbol=sym, severity="WARNING"); return
        cash -= cost
        positions[sym] = Position(
            symbol=sym, quantity=qty, entry_fill=entry_fill, initial_stop=initial_stop,
            entry_ts=decision_avail, entry_fill_ts=ot, entry_decision_id=decision_id,
            entry_commission=comm, entry_slippage=slip, expected_rps=expected_rps, actual_rps=actual_rps)
        add_event("ENTRY_FILLED", ts=ot, symbol=sym, quantity=qty, fill=_q(entry_fill),
                  initial_stop=_q(initial_stop), expected_rps=_q(expected_rps), actual_rps=_q(actual_rps))

    # Group events by availability timestamp; a group is one coherent event-time step.
    groups: dict[str, list[tuple[str, int]]] = {}
    for sym in symbols:
        for idx, bar in enumerate(bars_by_symbol.get(sym, [])):
            groups.setdefault(cal.available_at(bar.ts, policy).isoformat(), []).append((sym, idx))

    for avail_iso in sorted(groups):
        group = sorted(groups[avail_iso])               # deterministic order within the step
        f_sym, f_idx = group[0]

        # PHASE 1 — DAY / SESSION BOUNDARY (reset daily state BEFORE any fill or risk gate)
        day = cal.parse_ts(bars_by_symbol[f_sym][f_idx].ts).astimezone(cal.NY).date().isoformat()
        if day != cur_day:
            cur_day = day
            day_start_equity = equity()                 # end-of-prior-day equity (pre-step marks)
            realized_day_start = realized

        # PHASE 2 — BAR_OPEN (pending fills, all using PRE-step marks; no cross-symbol close leak)
        for sym, idx in group:
            bars = bars_by_symbol[sym]
            bar = bars[idx]
            pend = pending.get(sym)
            if pend and pend["fill_idx"] == idx:
                prev_ts = bars[idx - 1].ts if idx > 0 else bar.ts
                ot = cal.bar_open_utc(bar.ts, policy).isoformat()
                if not cal.is_contiguous(prev_ts, bar.ts, policy):
                    add_event("GAP_BLOCKED_FILL", ts=ot, symbol=sym, severity="WARNING",
                              from_ts=prev_ts, to_ts=bar.ts)
                elif pend["kind"] == ENTER and sym not in positions:
                    try_enter(sym, bar, pend["decision_avail"], pend["decision_id"], pend["evidence"])
                elif pend["kind"] == EXIT and sym in positions:
                    do_exit(sym, bar.open, "SIGNAL_EXIT", pend["decision_avail"], ot, pend["decision_id"])
                pending.pop(sym, None)

        # PHASE 3 — INTRABAR_STOP (gap-aware; conservative intrabar fill time = bar open)
        for sym, idx in group:
            if sym in positions:
                positions[sym].bars_held += 1
                bar = bars_by_symbol[sym][idx]
                pos = positions[sym]
                if bar.low <= pos.initial_stop:
                    fill_ref = bar.open if bar.open <= pos.initial_stop else pos.initial_stop
                    ot = cal.bar_open_utc(bar.ts, policy).isoformat()
                    do_exit(sym, fill_ref, "STOP", ot, ot, None)

        # PHASE 4 — BAR_CLOSE / AVAILABLE (marks become current only now)
        for sym, idx in group:
            marks[sym] = bars_by_symbol[sym][idx].close

        # PHASE 5 — STRATEGY_DECISION (schedule the next-bar-open fill)
        for sym, idx in group:
            bars = bars_by_symbol[sym]
            dec = strategy.decide(PitContext(sym, bars[: idx + 1]))
            seq["dec"] += 1
            dec_id = f"d{seq['dec']}"
            res.decisions.append({
                "id": dec_id, "seq": seq["dec"], "ts": dec.ts, "symbol": sym,
                "strategy_id": dec.strategy_id, "strategy_version": dec.strategy_version, "action": dec.action,
                "confidence": dec.confidence, "evidence": {k: str(v) for k, v in dec.evidence.items()},
                "missing_inputs": dec.missing_inputs, "reason": dec.reason, "checksum": dec.checksum()})
            nxt = idx + 1
            if nxt < len(bars) and sym not in pending:
                if dec.action == ENTER_LONG and sym not in positions:
                    pending[sym] = {"kind": ENTER, "fill_idx": nxt, "decision_id": dec_id,
                                    "evidence": dict(dec.evidence), "decision_avail": avail_iso}
                elif dec.action == "EXIT" and sym in positions:
                    pending[sym] = {"kind": EXIT, "fill_idx": nxt, "decision_id": dec_id,
                                    "evidence": {}, "decision_avail": avail_iso}

        # PHASE 6 — PORTFOLIO_SNAPSHOT (one coherent equity point for the step)
        eq = equity()
        peak_equity = max(peak_equity, eq)
        daily = eq - day_start_equity
        gp = (gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z
        dd = ((peak_equity - eq) / peak_equity * _HUNDRED) if peak_equity > 0 else _Z
        seq["equity"] += 1
        res.equity_points.append({
            "seq": seq["equity"], "ts": avail_iso, "cash": _q(cash), "equity": _q(eq),
            "realized_pnl": _q(realized), "unrealized_pnl": _q(unrealized()), "daily_pnl": _q(daily),
            "gross_exposure_pct": round(float(gp), 4), "net_exposure_pct": round(float(gp), 4),
            "drawdown_pct": round(float(dd), 4)})

    # End-of-test liquidation — explicit + costed, at each open position's last available bar close.
    had_open = bool(positions)
    for sym in list(positions):
        last = bars_by_symbol[sym][-1]
        at = cal.available_at(last.ts, policy).isoformat()
        do_exit(sym, last.close, "EOT_LIQUIDATION", at, at, None)
        add_event("EOT_LIQUIDATION", ts=at, symbol=sym)
    if had_open and res.equity_points:
        peak_equity = max(peak_equity, cash)
        dd = ((peak_equity - cash) / peak_equity * _HUNDRED) if peak_equity > 0 else _Z
        seq["equity"] += 1
        res.equity_points.append({
            "seq": seq["equity"], "ts": res.equity_points[-1]["ts"], "cash": _q(cash), "equity": _q(cash),
            "realized_pnl": _q(realized), "unrealized_pnl": _q(_Z), "daily_pnl": None,
            "gross_exposure_pct": 0.0, "net_exposure_pct": 0.0, "drawdown_pct": round(float(dd), 4)})
    res.ending_equity = _q(cash)
    return res
