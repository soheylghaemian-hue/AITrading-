"""Deterministic sentiment + impact analysis (§ Phase G2.1).

PURE functions. Sentiment is either the provider's own per-ticker label (Polygon `insights`, a REAL
signal) or, absent that, a transparent lexicon score computed from the article's own title+summary.
Impact is a transparent keyword classifier. Nothing is fabricated — every output is a function of the
real text (or the provider's real label). No LLM call, no network, no randomness → fully testable.
"""
from __future__ import annotations

# Small, transparent finance lexicons. Intentionally conservative; ambiguous text → neutral / LOW.
POSITIVE_WORDS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "gain", "gains",
    "record", "growth", "profit", "upgrade", "upgraded", "outperform", "bullish", "strong",
    "raises", "raised", "tops", "jumps", "rebound", "optimistic", "expands", "wins", "approval",
}
NEGATIVE_WORDS = {
    "miss", "misses", "plunge", "plunges", "slump", "drop", "drops", "fall", "falls", "decline",
    "loss", "losses", "downgrade", "downgraded", "underperform", "bearish", "weak", "cuts", "cut",
    "warning", "warns", "lawsuit", "probe", "investigation", "recall", "bankruptcy", "fraud", "halts",
}
HIGH_IMPACT = {
    "earnings", "guidance", "downgrade", "upgrade", "merger", "acquisition", "acquires", "buyout",
    "bankruptcy", "sec", "lawsuit", "recall", "fda", "halt", "halts", "fraud", "ceo", "dividend",
    "split", "profit warning", "restructuring", "layoffs",
}
MEDIUM_IMPACT = {
    "revenue", "forecast", "outlook", "analyst", "price target", "partnership", "contract", "launch",
    "product", "expansion", "buyback", "guidance cut", "rating",
}

_POS = 0.6
_NEG = -0.6


def sentiment_label(score: float | None) -> str | None:
    """Map a numeric sentiment score to positive / neutral / negative (None → None)."""
    if score is None:
        return None
    if score > 0.15:
        return "positive"
    if score < -0.15:
        return "negative"
    return "neutral"


def analyze_sentiment(text: str, provider_sentiment: str | None = None) -> tuple[float, str]:
    """(score, label). The provider's real per-ticker sentiment wins; otherwise a lexicon score on the
    real text. No text signal → neutral 0.0 (never a fabricated conviction)."""
    ps = (provider_sentiment or "").lower()
    if ps in ("positive", "negative", "neutral"):
        score = {"positive": _POS, "negative": _NEG, "neutral": 0.0}[ps]
        return score, ps
    t = (text or "").lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in t)
    neg = sum(1 for w in NEGATIVE_WORDS if w in t)
    if pos == 0 and neg == 0:
        return 0.0, "neutral"
    score = round((pos - neg) / (pos + neg), 4)
    return score, sentiment_label(score) or "neutral"


def classify_impact(text: str) -> str:
    """LOW / MEDIUM / HIGH from transparent keyword signals in the real text."""
    t = (text or "").lower()
    if any(w in t for w in HIGH_IMPACT):
        return "HIGH"
    if any(w in t for w in MEDIUM_IMPACT):
        return "MEDIUM"
    return "LOW"
