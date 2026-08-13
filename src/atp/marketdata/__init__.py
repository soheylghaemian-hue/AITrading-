"""Provider-independent market-data layer (§ Phase 10).

A global instrument universe → contract resolution → NormalizedQuote → data-quality gate. The
autonomous pipeline consumes ONLY normalized, quality-gated quotes — it never touches IBKR
directly, never sees delayed/stale/invalid/fabricated prices.
"""

from .manager import MarketDataManager
from .quality import QualityStatus, quality_gate
from .quote import NormalizedQuote
from .universe import GLOBAL_UNIVERSE, InstrumentSpec

__all__ = [
    "MarketDataManager", "QualityStatus", "quality_gate", "NormalizedQuote",
    "GLOBAL_UNIVERSE", "InstrumentSpec",
]
