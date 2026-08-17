"""§ R3.0 — deterministic replay engine: coverage preflight + Decimal simulated fills/accounting.

Pure, seedless-deterministic (same bars + config ⇒ identical result). Internal-only fills: a "trade"
is a research record — NO broker/order/execution object is ever created. Correction #2 risk sizing:
`risk_budget = equity × risk_per_trade_pct/100`, `risk_per_share = expected_entry_ref − initial_stop`
(= atr_stop_mult · ATR by construction), `risk_qty = floor(risk_budget / risk_per_share)`, then capped
by cash (incl. costs), gross-exposure limit, concurrent-position limit and the integer-share rule.
`risk_per_share ≤ 0` ⇒ entry rejected INVALID_STOP_DISTANCE. Fills: next eligible bar OPEN; gap through
stop fills at the adverse open; ambiguous intrabar stop/target ⇒ stop first (only reachable once a
strategy defines a target — the baseline has none). EOT liquidation is explicit and costed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal

from . import calendars as cal
from .risk_adapter import evaluate_sim_risk
from .strategy import ENTER_LONG, EXIT, PitContext, ResearchBar, ResearchStrategy

_Z = Decimal(0)
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
ENTER, EXIT = "ENTER", "EXIT"       # internal pending-fill kinds (distinct from strategy actions)


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENT)


# --------------------------------------------------------------------------- coverage preflight
@dataclass(slots=True)
class SymbolCoverage:
    symbol: str
    requested_start: str
    requested_end: str
    first_available_ts: str | None
    last_available_ts: str | None
    expected_bars: int
    available_bars: int
    missing_bars: int
    missing_ratio: float
    warmup_bars: int
    usable_bars: int
    duplicate_bars: int
    out_of_order_bars: int
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
    rows: list[SymbolCoverage] = []
    for sym in symbols:
        bars = bars_by_symbol.get(sym, [])
        avail = len(bars)
        expected = cal.expected_bars(start_dt, end_dt, policy)
        dup = avail - len({b.ts for b in bars})
        ooo = sum(1 for i in range(1, avail) if bars[i].ts < bars[i - 1].ts)
        usable = max(0, avail - warmup)
        missing = max(0, expected - avail)
        ratio = (missing / expected) if expected > 0 else 1.0
        coverage = (Decimal(avail) / Decimal(expected)) if expected > 0 else _Z
        reason = None
        if avail < min_bars:
            reason = f"only {avail} bars (< {min_bars} required)"
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
        rows.append(SymbolCoverage(
            symbol=sym, requested_start=start_dt.isoformat(), requested_end=end_dt.isoformat(),
            first_available_ts=(bars[0].ts if bars else None), last_available_ts=(bars[-1].ts if bars else None),
            expected_bars=expected, available_bars=avail, missing_bars=missing, missing_ratio=round(ratio, 4),
            warmup_bars=warmup, usable_bars=usable, duplicate_bars=dup, out_of_order_bars=ooo,
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
    entry_ts: str
    entry_fill_ts: str
    entry_decision_id: str
    entry_commission: Decimal
    entry_slippage: Decimal
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


def _floor_int(v: Decimal) -> Decimal:
    return v.to_integral_value(rounding=ROUND_DOWN)


def replay(*, symbols: list[str], bars_by_symbol: dict[str, list[ResearchBar]],
           policy: cal.AvailabilityPolicy, strategy: ResearchStrategy, risk_config: dict, costs: Costs,
           starting_capital: Decimal, max_concurrent: int) -> ReplayResult:
    """Chronological, multi-symbol, long-only replay. `risk_config` carries Decimal capital and pct
    limits already snapshotted from the canonical risk config."""
    res = ReplayResult(starting_capital=starting_capital)
    cash = starting_capital
    realized = _Z
    peak_equity = starting_capital
    positions: dict[str, Position] = {}
    pending: dict[str, dict] = {}          # symbol -> {kind, fill_idx, decision_id, evidence}
    marks: dict[str, Decimal] = {}
    seq = {"dec": 0, "trade": 0, "equity": 0, "event": 0}
    risk_pct = Decimal(str(risk_config["max_position_risk_pct"]))
    max_expo_pct = Decimal(str(risk_config["max_portfolio_exposure_pct"]))
    max_daily_loss = Decimal(str(risk_config["capital"])) * Decimal(str(risk_config["max_daily_loss_pct"])) / _HUNDRED
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

    # Merged, deterministic event order: (availability time, symbol). Ties broken by symbol name.
    events: list[tuple[str, str, int]] = []
    for sym in symbols:
        for idx, bar in enumerate(bars_by_symbol.get(sym, [])):
            events.append((cal.available_at(bar.ts, policy).isoformat(), sym, idx))
    events.sort(key=lambda e: (e[0], e[1]))

    def do_exit(sym, ref_price, reason, avail_iso, exit_ts, exit_decision_id, ambiguous=False):
        nonlocal cash, realized
        pos = positions.pop(sym)
        fill = costs.sell_fill(ref_price)
        comm = costs.commission(pos.quantity)
        slip = costs.exec_slippage(ref_price, pos.quantity)
        proceeds = pos.quantity * fill
        cash += proceeds - comm
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
            "exit_fill_ts": exit_ts, "exit_price": _q(fill), "quantity": pos.quantity,
            "gross_pnl": _q(gross), "commission": _q(total_comm), "slippage": _q(total_slip),
            "net_pnl": _q(net), "return_pct": round(float(ret * _HUNDRED), 4), "bars_held": pos.bars_held,
            "exit_reason": reason, "ambiguous": ambiguous})

    def try_enter(sym, bar, avail_iso, decision_id, evidence):
        nonlocal cash
        eq = equity()
        # Risk-state gate via the pure evaluator over the simulated portfolio.
        rs = evaluate_sim_risk(
            risk_config, realized=(realized - realized_day_start), unrealized=unrealized(),
            ts=avail_iso, peak_equity=peak_equity, equity=eq,
            gross_pct=(gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z,
            net_pct=(gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z)
        if rs["status"] == "BLOCKED":
            add_event("RISK_BLOCKED", ts=bar.ts, symbol=sym, severity="WARNING",
                      reasons=rs["reasons"]); return
        if rs["status"] == "WARNING":
            add_event("RISK_WARNING", ts=bar.ts, symbol=sym, reasons=rs["reasons"])
        if len(positions) >= max_concurrent:
            add_event("MAX_CONCURRENT_REACHED", ts=bar.ts, symbol=sym, severity="WARNING",
                      max_concurrent=max_concurrent); return
        rps = Decimal(str(evidence["risk_per_share"]))
        if rps <= 0:
            add_event("INVALID_STOP_DISTANCE", ts=bar.ts, symbol=sym, severity="WARNING",
                      risk_per_share=rps); return
        risk_budget = eq * risk_pct / _HUNDRED
        entry_fill = costs.buy_fill(bar.open)
        risk_qty = _floor_int(risk_budget / rps)
        # exposure cap
        headroom = eq * max_expo_pct / _HUNDRED - gross_exposure()
        qty_expo = _floor_int(headroom / entry_fill) if entry_fill > 0 else _Z
        # cash cap incl. costs (commission ≥ min or per-share)
        qty_cash = _floor_int((cash - costs.min_commission) / (entry_fill + costs.commission_per_share)) \
            if entry_fill > 0 else _Z
        qty = min(risk_qty, qty_expo, qty_cash)
        if qty <= 0:
            binding = "EXPOSURE_REJECTED" if qty_expo <= 0 else "INSUFFICIENT_CASH"
            add_event(binding, ts=bar.ts, symbol=sym, severity="WARNING",
                      risk_qty=risk_qty, qty_expo=qty_expo, qty_cash=qty_cash); return
        comm = costs.commission(qty)
        slip = costs.exec_slippage(bar.open, qty)
        cost = qty * entry_fill + comm
        if cost > cash:                      # final integer guard (never negative cash)
            add_event("INSUFFICIENT_CASH", ts=bar.ts, symbol=sym, severity="WARNING"); return
        cash -= cost
        positions[sym] = Position(
            symbol=sym, quantity=qty, entry_fill=entry_fill, initial_stop=Decimal(str(evidence["initial_stop"])),
            entry_ts=evidence["decision_ts"], entry_fill_ts=bar.ts, entry_decision_id=decision_id,
            entry_commission=comm, entry_slippage=slip)
        marks[sym] = bar.close
        add_event("ENTRY_FILLED", ts=bar.ts, symbol=sym, quantity=qty, fill=_q(entry_fill),
                  initial_stop=_q(Decimal(str(evidence["initial_stop"]))))

    for avail_iso, sym, idx in events:
        bars = bars_by_symbol[sym]
        bar = bars[idx]
        # (1) execute pending fill scheduled for THIS bar (next-bar-open rule)
        pend = pending.get(sym)
        if pend and pend["fill_idx"] == idx:
            prev_ts = bars[idx - 1].ts if idx > 0 else bar.ts
            if not cal.is_contiguous(prev_ts, bar.ts, policy):
                add_event("GAP_BLOCKED_FILL", ts=bar.ts, symbol=sym, severity="WARNING",
                          from_ts=prev_ts, to_ts=bar.ts)
            elif pend["kind"] == ENTER and sym not in positions:
                try_enter(sym, bar, avail_iso, pend["decision_id"], pend["evidence"])
            elif pend["kind"] == EXIT and sym in positions:
                do_exit(sym, bar.open, "SIGNAL_EXIT", avail_iso, bar.ts, pend["decision_id"])
            pending.pop(sym, None)
        # (2) intrabar stop check (gap-aware)
        if sym in positions:
            positions[sym].bars_held += 1
            pos = positions[sym]
            if bar.low <= pos.initial_stop:
                fill_ref = bar.open if bar.open <= pos.initial_stop else pos.initial_stop
                do_exit(sym, fill_ref, "STOP", avail_iso, bar.ts, None)
        # (3) strategy decision on completed bars up to this bar
        dec = strategy.decide(PitContext(sym, bars[: idx + 1]))
        seq["dec"] += 1
        dec_id = f"d{seq['dec']}"
        res.decisions.append({
            "id": dec_id, "seq": seq["dec"], "ts": dec.ts, "symbol": sym,
            "strategy_id": dec.strategy_id, "strategy_version": dec.strategy_version, "action": dec.action,
            "confidence": dec.confidence, "evidence": {k: (str(v)) for k, v in dec.evidence.items()},
            "missing_inputs": dec.missing_inputs, "reason": dec.reason, "checksum": dec.checksum()})
        nxt = idx + 1
        if nxt < len(bars) and sym not in pending:
            if dec.action == ENTER_LONG and sym not in positions:
                ev = dict(dec.evidence); ev["decision_ts"] = dec.ts
                pending[sym] = {"kind": ENTER, "fill_idx": nxt, "decision_id": dec_id, "evidence": ev}
            elif dec.action == EXIT and sym in positions:
                pending[sym] = {"kind": EXIT, "fill_idx": nxt, "decision_id": dec_id, "evidence": {}}
        # (4) mark-to-market + equity point + daily/day-boundary tracking
        marks[sym] = bar.close
        day = cal.parse_ts(bar.ts).astimezone(cal.NY).date().isoformat()
        if day != cur_day:
            cur_day = day
            day_start_equity = equity()
            realized_day_start = realized
        eq = equity()
        peak_equity = max(peak_equity, eq)
        daily = eq - day_start_equity
        # daily-loss hard block is enforced through the risk gate (evaluate_sim_risk) at entry time.
        _ = max_daily_loss
        gp = (gross_exposure() / eq * _HUNDRED) if eq > 0 else _Z
        dd = ((peak_equity - eq) / peak_equity * _HUNDRED) if peak_equity > 0 else _Z
        seq["equity"] += 1
        res.equity_points.append({
            "seq": seq["equity"], "ts": avail_iso, "cash": _q(cash), "equity": _q(eq),
            "realized_pnl": _q(realized), "unrealized_pnl": _q(unrealized()), "daily_pnl": _q(daily),
            "gross_exposure_pct": round(float(gp), 4), "net_exposure_pct": round(float(gp), 4),
            "drawdown_pct": round(float(dd), 4)})

    # End-of-test liquidation — explicit and costed, at each open position's last available bar close.
    had_open = bool(positions)
    for sym in list(positions):
        last = bars_by_symbol[sym][-1]
        do_exit(sym, last.close, "EOT_LIQUIDATION", cal.available_at(last.ts, policy).isoformat(),
                last.ts, None)
        add_event("EOT_LIQUIDATION", ts=last.ts, symbol=sym)
    # Final settlement equity point: after liquidation everything is cash, so equity == cash.
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
