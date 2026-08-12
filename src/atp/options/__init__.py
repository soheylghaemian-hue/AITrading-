"""Options & volatility (§5 Derivate): Black–Scholes pricing, greeks, IV, chain analytics."""

from .chain import ChainFeatures, OptionChain, OptionQuote, build_chain, compute_features
from .engine import OptionsEngine
from .execution import (
    Combo,
    ComboResult,
    OptionLeg,
    Settlement,
    execute_combo,
    option,
    settle_expiration,
    straddle,
    strangle,
    vertical_call_spread,
    vertical_put_spread,
)
from .pricing import Greeks, black_scholes, greeks, implied_vol

__all__ = [
    "black_scholes",
    "greeks",
    "Greeks",
    "implied_vol",
    "OptionQuote",
    "OptionChain",
    "ChainFeatures",
    "compute_features",
    "build_chain",
    "OptionsEngine",
    "option",
    "OptionLeg",
    "Combo",
    "ComboResult",
    "execute_combo",
    "settle_expiration",
    "Settlement",
    "vertical_call_spread",
    "vertical_put_spread",
    "straddle",
    "strangle",
]
