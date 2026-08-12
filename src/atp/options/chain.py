"""Option chain and chain-level analytics (§5 Derivate: IV, Greeks, Put/Call, Skew, Gamma).

Turns a set of option quotes into the derivative signals the concept lists: at-the-money IV,
volatility skew (are puts bid over calls?), put/call ratios, and a dealer gamma-exposure proxy.
`build_chain` synthesizes a chain from a spot + a base IV + a skew slope using the Black–Scholes
greeks, so tests and demos exercise the whole stack without a live options feed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import greeks


@dataclass(slots=True)
class OptionQuote:
    strike: float
    right: str            # "C" or "P"
    iv: float
    delta: float = 0.0
    gamma: float = 0.0
    open_interest: float = 0.0
    volume: float = 0.0


@dataclass(slots=True)
class OptionChain:
    underlying: str       # instrument key of the underlying
    spot: float
    T: float              # years to expiry
    quotes: list[OptionQuote]


@dataclass(slots=True)
class ChainFeatures:
    underlying: str
    spot: float
    atm_iv: float
    iv_skew: float             # OTM-put IV minus OTM-call IV (positive => put skew)
    put_call_oi_ratio: float
    put_call_volume_ratio: float
    gamma_exposure: float      # dealer gamma proxy (sign: net long-gamma if positive)
    atm_strike: float

    def as_dict(self) -> dict:
        return {
            "underlying": self.underlying, "spot": self.spot, "atm_iv": self.atm_iv,
            "iv_skew": self.iv_skew, "put_call_oi_ratio": self.put_call_oi_ratio,
            "put_call_volume_ratio": self.put_call_volume_ratio,
            "gamma_exposure": self.gamma_exposure, "atm_strike": self.atm_strike,
        }


def _nearest(quotes: list[OptionQuote], target: float, right: str) -> OptionQuote | None:
    cands = [q for q in quotes if q.right == right]
    return min(cands, key=lambda q: abs(q.strike - target)) if cands else None


def compute_features(chain: OptionChain) -> ChainFeatures:
    spot = chain.spot
    calls = [q for q in chain.quotes if q.right == "C"]
    puts = [q for q in chain.quotes if q.right == "P"]

    atm_call = _nearest(calls, spot, "C")
    atm_put = _nearest(puts, spot, "P")
    atm_ivs = [q.iv for q in (atm_call, atm_put) if q is not None]
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else 0.0
    atm_strike = atm_call.strike if atm_call else (atm_put.strike if atm_put else spot)

    # Skew: 5%-OTM put IV vs 5%-OTM call IV.
    otm_put = _nearest(puts, spot * 0.95, "P")
    otm_call = _nearest(calls, spot * 1.05, "C")
    iv_skew = ((otm_put.iv if otm_put else atm_iv) - (otm_call.iv if otm_call else atm_iv))

    call_oi = sum(q.open_interest for q in calls)
    put_oi = sum(q.open_interest for q in puts)
    call_vol = sum(q.volume for q in calls)
    put_vol = sum(q.volume for q in puts)
    pc_oi = (put_oi / call_oi) if call_oi > 0 else 0.0
    pc_vol = (put_vol / call_vol) if call_vol > 0 else 0.0

    # Dealer gamma proxy: assume dealers are short calls / long puts (sign convention below).
    gex = spot * 100.0 * (
        sum(q.gamma * q.open_interest for q in calls) - sum(q.gamma * q.open_interest for q in puts)
    )

    return ChainFeatures(
        underlying=chain.underlying, spot=spot, atm_iv=atm_iv, iv_skew=iv_skew,
        put_call_oi_ratio=pc_oi, put_call_volume_ratio=pc_vol,
        gamma_exposure=gex, atm_strike=atm_strike,
    )


def build_chain(
    underlying: str,
    spot: float,
    T: float,
    *,
    base_iv: float = 0.20,
    skew: float = 0.0,          # extra IV per 1.0 of (K/spot - 1) below spot (put skew)
    r: float = 0.0,
    strikes: list[float] | None = None,
    oi_call: float = 100.0,
    oi_put: float = 100.0,
) -> OptionChain:
    """Synthesize a chain around `spot` with a linear skew, filling greeks from Black–Scholes."""
    if strikes is None:
        strikes = [spot * m for m in (0.90, 0.95, 1.00, 1.05, 1.10)]
    quotes: list[OptionQuote] = []
    for K in strikes:
        moneyness = K / spot - 1.0
        iv = max(0.01, base_iv - skew * moneyness)   # lower strikes (moneyness<0) => higher IV
        for right in ("C", "P"):
            g = greeks(spot, K, T, r, iv, right)
            quotes.append(OptionQuote(
                strike=K, right=right, iv=iv, delta=g.delta, gamma=g.gamma,
                open_interest=oi_call if right == "C" else oi_put,
                volume=(oi_call if right == "C" else oi_put) * 0.5,
            ))
    return OptionChain(underlying=underlying, spot=spot, T=T, quotes=quotes)
