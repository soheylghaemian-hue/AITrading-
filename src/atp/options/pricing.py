"""Black–Scholes pricing, Greeks and implied volatility (§5 Derivate).

Exact, textbook option maths in pure stdlib (`math`) — no numpy — so it runs in the offline
suite and every number is auditable. European options, continuous compounding, optional
continuous dividend/carry yield `q`. This is the foundation the derivative data layer (IV,
skew, gamma exposure) and the volatility specialist build on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    vol = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol
    return d1, d1 - vol


def _is_call(right: str) -> bool:
    r = right.upper()
    if r in ("C", "CALL"):
        return True
    if r in ("P", "PUT"):
        return False
    raise ValueError(f"right must be call/put, got {right!r}")


def black_scholes(S: float, K: float, T: float, r: float, sigma: float, right: str, q: float = 0.0) -> float:
    """Price of a European option. Handles the T→0 / σ→0 limit as intrinsic value."""
    call = _is_call(right)
    if T <= 0 or sigma <= 0:
        intrinsic = (S - K) if call else (K - S)
        return max(0.0, intrinsic) * math.exp(-r * 0.0)  # already at expiry
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    df_r, df_q = math.exp(-r * T), math.exp(-q * T)
    if call:
        return S * df_q * _norm_cdf(d1) - K * df_r * _norm_cdf(d2)
    return K * df_r * _norm_cdf(-d2) - S * df_q * _norm_cdf(-d1)


@dataclass(slots=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float          # per 1.00 (100 vol points); divide by 100 for per-vol-point
    theta: float         # per year; divide by 365 for per-day
    rho: float


def greeks(S: float, K: float, T: float, r: float, sigma: float, right: str, q: float = 0.0) -> Greeks:
    call = _is_call(right)
    price = black_scholes(S, K, T, r, sigma, right, q)
    if T <= 0 or sigma <= 0:
        delta = (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
        return Greeks(price=price, delta=delta, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    df_r, df_q = math.exp(-r * T), math.exp(-q * T)
    pdf = _norm_pdf(d1)
    gamma = df_q * pdf / (S * sigma * math.sqrt(T))
    vega = S * df_q * pdf * math.sqrt(T)
    if call:
        delta = df_q * _norm_cdf(d1)
        theta = (-S * df_q * pdf * sigma / (2 * math.sqrt(T))
                 - r * K * df_r * _norm_cdf(d2) + q * S * df_q * _norm_cdf(d1))
        rho = K * T * df_r * _norm_cdf(d2)
    else:
        delta = -df_q * _norm_cdf(-d1)
        theta = (-S * df_q * pdf * sigma / (2 * math.sqrt(T))
                 + r * K * df_r * _norm_cdf(-d2) - q * S * df_q * _norm_cdf(-d1))
        rho = -K * T * df_r * _norm_cdf(-d2)
    return Greeks(price=price, delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def implied_vol(price: float, S: float, K: float, T: float, r: float, right: str,
                q: float = 0.0, *, tol: float = 1e-6, max_iter: int = 100) -> float | None:
    """Recover σ from a market price. Newton with a bisection fallback; None if no solution.

    Returns None when the price is below intrinsic or outside the achievable range (arbitrage
    or bad data) — an honest 'cannot invert' rather than a fabricated number."""
    call = _is_call(right)
    if T <= 0:
        return None
    df_r, df_q = math.exp(-r * T), math.exp(-q * T)
    intrinsic = max(0.0, (S * df_q - K * df_r) if call else (K * df_r - S * df_q))
    upper = S * df_q if call else K * df_r
    if price < intrinsic - tol or price > upper + tol:
        return None

    sigma = 0.2
    for _ in range(max_iter):
        g = greeks(S, K, T, r, sigma, right, q)
        diff = g.price - price
        if abs(diff) < tol:
            return sigma
        if g.vega < 1e-8:
            break
        sigma -= diff / g.vega
        if sigma <= 0 or sigma > 10:
            break

    # Bisection fallback on a wide bracket.
    lo, hi = 1e-4, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        diff = black_scholes(S, K, T, r, mid, right, q) - price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
