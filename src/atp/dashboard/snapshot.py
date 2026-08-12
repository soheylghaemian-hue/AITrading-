"""Dashboard read-model (§22 — the Command Center).

Assembles ONE JSON-able snapshot of the whole desk from the live objects: capital, positions,
risk, per-instrument regimes, the AI-team (agents) performance, governance, system health,
mode, the discovery funnel and notifications. The FastAPI layer (`api.py`) is a thin serializer
on top; any frontend renders exactly this shape.

Every field is derived from REAL state — positions from the broker, P&L from the account, edge
from recorded trades, health from the engines. Nothing is fabricated (§33): where there is no
data yet, fields are `None`/empty (the UI shows NO DATA), never invented performance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from ..brokers.base import Account
from ..governance.registry import StrategyRegistry
from ..journal.analytics import TradeAnalytics
from ..journal.store import TradeJournal
from ..risk.config import TradingRiskConfig, trading_risk_view
from ..risk.engine import RiskEngine

# The nine+ specialists the AI team is expected to field (§9). Used to show IDLE agents too.
AGENT_NAMES = [
    "momentum", "mean_reversion", "breakout", "macro", "volatility",
    "fx_carry", "stat_arb", "event", "cross_asset",
]


@dataclass(slots=True)
class RiskView:
    halted: bool
    halt_reason: str
    killed: bool
    kill_reason: str
    broker_connected: bool
    day_start_equity: float
    peak_equity: float
    drawdown: float          # fraction from peak
    daily_pnl: float         # currency, vs day start
    daily_loss_pct: float    # current daily loss as a fraction (>=0)
    gross_leverage: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_gross_leverage: float
    max_position_pct: float
    max_correlated_exposure_pct: float


def _risk_view(risk: RiskEngine, account: Account) -> RiskView:
    st, lim = risk.state, risk.limits
    equity = account.equity
    dd = (st.peak_equity - equity) / st.peak_equity if st.peak_equity > 0 else 0.0
    daily = equity - st.day_start_equity
    daily_loss = max(0.0, -daily / st.day_start_equity) if st.day_start_equity > 0 else 0.0
    return RiskView(
        halted=st.halted, halt_reason=st.halt_reason, killed=st.killed, kill_reason=st.kill_reason,
        broker_connected=st.broker_connected,
        day_start_equity=st.day_start_equity, peak_equity=st.peak_equity,
        drawdown=dd, daily_pnl=daily, daily_loss_pct=daily_loss,
        gross_leverage=account.gross_leverage,
        max_daily_loss_pct=lim.max_daily_loss_pct, max_drawdown_pct=lim.max_drawdown_pct,
        max_gross_leverage=lim.max_gross_leverage, max_position_pct=lim.max_position_pct,
        max_correlated_exposure_pct=lim.max_correlated_exposure_pct,
    )


def _positions(account: Account) -> list[dict]:
    out = []
    for key, p in account.positions.items():
        out.append({
            "key": key, "symbol": p.instrument.symbol,
            "asset_class": p.instrument.asset_class.value,
            "quantity": p.quantity, "avg_price": p.avg_price, "market_price": p.market_price,
            "notional": p.notional, "unrealized_pnl": p.unrealized_pnl,
            "side": "long" if p.quantity > 0 else "short",
        })
    out.sort(key=lambda d: abs(d["notional"]), reverse=True)
    return out


def _agents(analytics: TradeAnalytics, registry: StrategyRegistry | None) -> list[dict]:
    """AI-team view: each specialist's status + recorded edge. Zeros where no trades yet."""
    by_name = {g.label: g for g in analytics.by_strategy()}
    names = set(AGENT_NAMES) | set(by_name)
    if registry is not None:
        names |= {s.name for s in registry.states()}
    out: list[dict] = []
    for name in sorted(names):
        g = by_name.get(name)
        state = registry.get(name) if registry is not None else None
        status = state.status.value if state is not None else ("active" if g else "idle")
        out.append({
            "name": name, "status": status,
            "trades": g.n_trades if g else 0,
            "win_rate": g.win_rate if g else None,
            "expectancy": g.expectancy if g else None,
            "total_pnl": g.total_pnl if g else None,
            "profit_factor": g.profit_factor if g else None,
            "avg_confidence": g.avg_confidence if g else None,
            "version": state.version if state is not None else None,
        })
    return out


def _system_status(risk: RiskView, data_ok: bool | None) -> str:
    if risk.killed or risk.halted:
        return "halted"
    if not risk.broker_connected or data_ok is False:
        return "degraded"
    return "online"


def _system_health(
    risk: RiskView, *, has_journal: bool, data_ok: bool | None,
    execution_enabled: bool = True, market_data_flag: str | None = None,
    historical_ok: bool | None = None,
) -> dict[str, str]:
    def flag(ok: bool | None) -> str:
        return "unknown" if ok is None else ("online" if ok else "offline")
    return {
        "trading_engine": "online",
        "risk_engine": "halted" if (risk.halted or risk.killed) else "online",
        # In read-only validation the execution path is deliberately not wired (§ Phase 2A).
        "execution_engine": "online" if execution_enabled else "disabled",
        "broker": "online" if risk.broker_connected else "offline",
        "market_data": market_data_flag if market_data_flag is not None else flag(data_ok),
        "historical_data": flag(historical_ok),
        "learning_engine": "online" if has_journal else "unknown",
        "database": "unknown",        # not observable from the read-model
        "redis": "unknown",
        "api": "online",
    }


def _hero(scan_funnel: dict | None) -> dict:
    """Discovery funnel counts (§4). None everywhere until a real scan is provided — no fakes."""
    keys = ("scanned", "opportunities", "after_liquidity", "after_statistical",
            "portfolio_approved", "risk_approved")
    return {k: (scan_funnel or {}).get(k) for k in keys}


# --- market-data availability (§10/§22) -------------------------------------------------------
#: The five states the Command Center distinguishes for every instrument's live quote.
QUOTE_STATES = ("DATA_AVAILABLE", "DELAYED", "STALE", "DATA_NOT_AVAILABLE", "ERROR")


def _present(v: object) -> bool:
    """A quote field counts as real only if it is a positive, finite number (NaN != NaN)."""
    try:
        return v is not None and v == v and float(v) > 0.0
    except (TypeError, ValueError):
        return False


def classify_quote(
    *, bid=None, ask=None, last=None, error_code: int | None = None, error_msg: str = "",
    delayed: bool = False, ts: datetime | None = None, now: datetime | None = None,
    stale_after: float = 30.0,
) -> tuple[str, str]:
    """Pure classifier → (status, reason). Never fabricates a price (§33).

    Precedence: a subscription/permission problem is reported as DATA_NOT_AVAILABLE with a
    clear reason (IBKR error 10089/10091/10167/10197 = market-data subscription required);
    any other hard error with no usable quote is ERROR; no usable quote is DATA_NOT_AVAILABLE;
    an old timestamp is STALE; a delayed feed is DELAYED; otherwise DATA_AVAILABLE.
    """
    subscription_codes = {10089, 10091, 10167}
    has_quote = _present(bid) or _present(ask) or _present(last)
    if error_code in subscription_codes or "subscription" in (error_msg or "").lower():
        return ("DATA_NOT_AVAILABLE", "IBKR market-data subscription required")
    if error_code == 10197 and not has_quote:
        return ("DATA_NOT_AVAILABLE", "no market data during a competing live session (transient)")
    if error_code is not None and not has_quote:
        return ("ERROR", error_msg or f"IBKR error {error_code}")
    if not has_quote:
        return ("DATA_NOT_AVAILABLE", "no quote (subscription required or market closed)")
    if ts is not None and now is not None:
        age = (now - ts).total_seconds()
        if age > stale_after:
            return ("STALE", f"last update {age:.0f}s ago")
    if delayed:
        return ("DELAYED", "delayed data (no real-time subscription)")
    return ("DATA_AVAILABLE", "live")


def _market_data_health(market_data: list[dict] | None) -> str:
    """Roll the per-instrument states up into one feed health flag for system health."""
    if not market_data:
        return "unknown"
    statuses = {row.get("status") for row in market_data}
    live = {"DATA_AVAILABLE", "DELAYED"}
    if statuses & live and (statuses - live):
        return "degraded"          # some instruments live, some not → PARTIAL
    if statuses & live:
        return "online"
    return "offline"


@dataclass(slots=True)
class DashboardSnapshot:
    generated_at: str
    mode: str                          # "paper" | "live"
    system_status: str                 # "online" | "degraded" | "halted"
    connected: bool | None             # broker/gateway connection (None = unknown)
    execution_enabled: bool            # false during read-only validation (§ Phase 2A)
    orders: int                        # orders sent this session — 0 in read-only mode
    account: dict
    risk: RiskView
    positions: list[dict]
    market: dict                       # per-instrument regime + features
    market_data: list[dict]            # per-instrument live-quote availability (5 states)
    subscriptions: list[dict]          # required/missing IBKR market-data subscriptions
    ai_analysis: list[dict]            # read-only agent observations/signals (NO execution)
    trading_risk: dict | None          # the 3-parameter TRADING RISK config + derived limits/status
    agents: list[dict]
    governance: list[dict]
    system_health: dict
    hero: dict
    analytics_overall: dict
    analytics_by_strategy: list[dict]
    analytics_by_regime: list[dict]
    recent_trades: list[dict]
    notifications: list[dict]
    n_trades: int

    def as_dict(self) -> dict:
        d = asdict(self)
        d["risk"] = asdict(self.risk)
        return d


def build_snapshot(
    *,
    account: Account,
    risk: RiskEngine,
    journal: TradeJournal | None = None,
    registry: StrategyRegistry | None = None,
    market: dict | None = None,
    mode: str = "paper",
    data_ok: bool | None = None,
    scan_funnel: dict | None = None,
    notifications: list | None = None,
    recent: int = 20,
    market_data: list[dict] | None = None,
    subscriptions: list[dict] | None = None,
    ai_analysis: list[dict] | None = None,
    buying_power: float | None = None,
    execution_enabled: bool = True,
    orders: int = 0,
    connected: bool | None = None,
    historical_ok: bool | None = None,
    risk_config: TradingRiskConfig | None = None,
    risk_capital: float | None = None,
) -> DashboardSnapshot:
    """Assemble the full Command Center state from live objects (§22). Real data only."""
    trades = journal.all() if journal is not None else []
    analytics = TradeAnalytics(trades)
    risk_view = _risk_view(risk, account)

    governance: list[dict] = []
    if registry is not None:
        for s in registry.states():
            governance.append({
                "name": s.name, "status": s.status.value,
                "version": s.version, "reason": s.reason, "since": s.since.isoformat(),
            })

    notif = [n.as_dict() if hasattr(n, "as_dict") else n for n in (notifications or [])]

    # TRADING RISK: the 3 user parameters + derived monetary limits + today's status. Sourced
    # from the authoritative Risk Engine limits (and the capital mandate), so it always reflects
    # what the engine actually enforces. Never fabricated — omitted if it can't be formed.
    cfg = risk_config
    if cfg is None:
        cap = risk_capital if (risk_capital and risk_capital > 0) else account.equity
        try:
            cfg = TradingRiskConfig(
                capital=cap, risk_per_trade_pct=risk.limits.max_trade_risk_pct,
                max_daily_loss_pct=risk.limits.max_daily_loss_pct,
            )
        except (ValueError, TypeError):
            cfg = None
    trading_risk = (
        trading_risk_view(cfg, daily_pnl=risk_view.daily_pnl,
                          halted=risk_view.halted or risk_view.killed)
        if cfg is not None else None
    )

    md_flag = _market_data_health(market_data)
    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        mode=mode,
        system_status=_system_status(risk_view, data_ok),
        connected=connected,
        execution_enabled=execution_enabled,
        orders=orders,
        account={
            "equity": account.equity, "cash": account.cash,
            "buying_power": buying_power,
            "realized_pnl": account.realized_pnl, "unrealized_pnl": account.unrealized_pnl,
            "gross_exposure": account.gross_exposure, "net_exposure": account.net_exposure,
            "gross_leverage": account.gross_leverage,
        },
        risk=risk_view,
        positions=_positions(account),
        market=market or {},
        market_data=market_data or [],
        subscriptions=subscriptions or [],
        ai_analysis=ai_analysis or [],
        trading_risk=trading_risk,
        agents=_agents(analytics, registry),
        governance=governance,
        system_health=_system_health(
            risk_view, has_journal=journal is not None, data_ok=data_ok,
            execution_enabled=execution_enabled,
            market_data_flag=md_flag if market_data is not None else None,
            historical_ok=historical_ok,
        ),
        hero=_hero(scan_funnel),
        analytics_overall=analytics.overall().as_dict(),
        analytics_by_strategy=[g.as_dict() for g in analytics.by_strategy()],
        analytics_by_regime=[g.as_dict() for g in analytics.by_regime()],
        recent_trades=[t.as_dict() for t in trades[-recent:][::-1]],
        notifications=notif,
        n_trades=len(trades),
    )
