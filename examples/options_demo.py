"""Demo: options pricing, greeks, chain analytics and the volatility specialist (§5/§8).

    PYTHONPATH=src python3 examples/options_demo.py

Prices an option and shows its greeks, builds a skewed chain and reads the §5 derivative
signals (ATM IV, skew, put/call, gamma), then walks IV up into a put-heavy panic and shows the
volatility specialist fading it. Every number is computed from Black–Scholes — nothing faked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from atp.core.enums import AssetClass, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.options import OptionsEngine, black_scholes, build_chain, compute_features, greeks, implied_vol
from atp.strategy.volatility import VolatilityStrategy

SPX = Instrument("SPX", AssetClass.INDEX)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _fs():
    return FeatureSet(instrument=SPX, ts=T0, price=100.0, n_bars=50, ready=True,
                      sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                      realized_vol=0.01, vol_percentile=0.9, rel_volume=1.0)


def main() -> None:
    print("=" * 66)
    print("  Options pricing & greeks (Black–Scholes, §5)")
    print("=" * 66)
    S, K, T, r, sig = 100.0, 100.0, 0.25, 0.02, 0.20
    g = greeks(S, K, T, r, sig, "C")
    print(f"  ATM call  S={S} K={K} T={T}y r={r:.0%} σ={sig:.0%}")
    print(f"    price={g.price:.4f}  delta={g.delta:.4f}  gamma={g.gamma:.5f}  "
          f"vega={g.vega/100:.4f}/volpt  theta={g.theta/365:.4f}/day")
    px = black_scholes(S, K, T, r, 0.20, "C")
    print(f"    implied vol from price {px:.4f} => {implied_vol(px, S, K, T, r, 'C'):.4f}  (recovers σ)")

    print("-" * 66)
    print("  Chain analytics (§5 Derivate): ATM IV, skew, put/call, gamma")
    feats = compute_features(build_chain(SPX.key, 100.0, 0.25, base_iv=0.20, skew=0.6,
                                         oi_put=250, oi_call=120))
    print(f"    ATM IV={feats.atm_iv:.2%}  skew={feats.iv_skew:+.3f}  "
          f"put/call OI={feats.put_call_oi_ratio:.2f}  GEX={feats.gamma_exposure:,.0f}")

    print("-" * 66)
    print("  Volatility specialist (§8): fade a put-heavy IV spike")
    eng = OptionsEngine()
    for iv in (0.12, 0.13, 0.14, 0.15, 0.16):
        eng.update(build_chain(SPX.key, 100.0, 0.25, base_iv=iv, oi_put=100, oi_call=100))
    eng.update(build_chain(SPX.key, 100.0, 0.25, base_iv=0.45, oi_put=300, oi_call=100))  # panic
    rank = eng.iv_rank(SPX.key)
    sig_ = VolatilityStrategy(eng, iv_rank_high=0.8, pc_high=1.3).generate(_fs(), Regime.HIGH_VOLATILITY)
    print(f"    IV rank={rank:.0%}  put/call={eng.features(SPX.key).put_call_oi_ratio:.1f}  "
          f"=> signal: {sig_.action.value.upper() if sig_ else 'none'} ({sig_.rationale if sig_ else '-'})")
    print("=" * 66)
    print("  NOTE: trades the underlying on options sentiment; direct multi-leg options")
    print("  execution (assignment, spreads) is a further step. IV→direction is a heuristic.")


if __name__ == "__main__":
    main()
