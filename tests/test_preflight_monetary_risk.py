"""Phase 11.5 — monetary-risk enforcement + explicit risk fields on decisions.

Proves the AUTHORITATIVE Risk Engine caps DOLLAR risk (not stop distance) at risk_per_trade × capital
and at the remaining daily-loss budget, and that the decision feed exposes monetary risk separately
from stop distance. No orders are placed."""

from atp.autonomous import PaperAutonomousEngine
from atp.brokers.base import Account, Order
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Instrument
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

CAP = 1_000_000.0


def _eng():
    e = RiskEngine(limits=RiskLimits(max_trade_risk_pct=0.01, max_daily_loss_pct=0.03),
                   state=RiskState(day_start_equity=CAP, peak_equity=CAP))
    e.state.broker_connected = True
    return e


def _acct(eq=CAP):
    return Account(cash=CAP, equity=eq, realized_pnl=eq - CAP, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions={})


def _order(sym, qty, side=Side.BUY, ac=AssetClass.EQUITY):
    return Order(instrument=Instrument(sym, ac), side=side, quantity=qty, order_type=OrderType.MARKET)


# ---- hard limit: monetary risk (not stop distance) is capped at 1% of capital ----
def test_risk_per_trade_dollar_cap_rejects():
    # notional 19.4% (< 20% position cap) but risk 250×45 = $11,250 > $10,000 (1%)
    d = _eng().check_order(_order("SPY", 250), _acct(), price=777.0, current_qty=0.0, stop_distance=45.0)
    assert not d.approved and "trade risk" in d.reason


def test_within_risk_cap_approved():
    d = _eng().check_order(_order("SPY", 100), _acct(), price=777.0, current_qty=0.0, stop_distance=3.89)
    assert d.approved


# ---- daily-loss budget: a trade may not risk more than what's left of the day's budget ----
def test_daily_budget_rejects():
    # after $28k loss remaining budget = $2,000; trade risks 100×25 = $2,500 (notional only 3%)
    d = _eng().check_order(_order("AAPL", 100), _acct(CAP - 28_000), price=300.0,
                           current_qty=0.0, stop_distance=25.0)
    assert not d.approved and "daily-loss budget" in d.reason


# ---- the Phase-11 sized trades all sit safely under the cap (notional-capped at 20%) ----
def test_phase11_style_trade_is_conservative():
    # SPY 257 @777.29, stop distance 3.886 → risk ≈ $999 = 0.10% << 1%
    d = _eng().check_order(_order("SPY", 257), _acct(), price=777.29, current_qty=0.0, stop_distance=3.886)
    assert d.approved
    monetary = 257 * 3.886
    assert monetary < 0.01 * CAP           # well under the $10k hard cap
    assert abs(monetary / CAP - 0.001) < 2e-4  # ≈ 0.10% of capital


# ---- FX (EUR.USD): quote currency == account currency, so risk = qty × stop distance (USD) ----
def test_fx_monetary_risk_uses_quote_currency():
    inst = Instrument("EUR", AssetClass.FX, currency="USD")
    assert inst.multiplier == 1
    qty, stop_dist = 173_493, 0.0057639     # ~ EUR.USD stop distance in USD
    monetary = qty * stop_dist * inst.multiplier
    assert abs(monetary - 1000.0) < 5.0     # ≈ $1,000, NOT computed with a share multiplier


# ---- the decision carries monetary risk SEPARATELY from stop distance ----
def test_decision_exposes_monetary_fields():
    class _Desk:  # minimal stand-in; we only exercise _decision_from
        pass
    eng = PaperAutonomousEngine.__new__(PaperAutonomousEngine)
    eng._journal_path = None
    d = {"instrument": "SPY", "action": "buy", "risk_decision": "APPROVED", "reason": "ok",
         "stop_distance": 3.886, "monetary_risk": 998.7, "risk_pct_capital": 0.0009987,
         "position_notional": 199_764.0, "entry": 777.29, "suggested_size": 257}
    md = [{"symbol": "SPY", "status": "DATA_AVAILABLE", "market_data_type": "REALTIME", "source": "MASSIVE"}]
    import datetime
    dec = eng._decision_from(d, md, datetime.datetime(2026, 8, 13, tzinfo=datetime.timezone.utc), "NO_ORDER (dry-run)")
    assert dec.stop_distance == 3.886          # per-share distance
    assert dec.monetary_risk == 998.7          # dollars
    assert round(dec.risk_pct_capital, 4) == 0.001
    assert dec.position_notional == 199_764.0
    assert dec.final_decision == "PAPER_TRADE_WOULD_BE_EXECUTED"
