"""Institutional Intelligence Enhancement (§ Phase R1.3) — READ-ONLY smart-money intelligence.

Two institutional signals on top of SEC 13F holdings:
  1. 13F quarter-over-quarter POSITION CHANGES (ACCUMULATION / REDUCTION / NEW_POSITION / EXIT) +
     an accumulation score — "is smart money adding or trimming?"
  2. SEC Form 4 INSIDER transactions (open-market BUY / SELL) + insider sentiment.

DATA ONLY. It never trades, never copy-trades, never generates orders, and never touches Trading Core /
Risk Engine / Broker / IBKR / Execution. Missing data → NO DATA (never fabricated).
"""

from .changes import accumulation_score, analyze_changes, net_share_change_pct
from .clusters import build_insider_cluster, detect_cluster, role_weight
from .collector import InstitutionalCollector
from .form4 import SecForm4Provider, parse_form4, parse_issuer_form4_refs
from .insider import insider_sentiment
from .readmodel import build_institutional_flow

__all__ = [
    "analyze_changes",
    "accumulation_score",
    "net_share_change_pct",
    "SecForm4Provider",
    "parse_form4",
    "parse_issuer_form4_refs",
    "insider_sentiment",
    "role_weight",
    "detect_cluster",
    "build_insider_cluster",
    "InstitutionalCollector",
    "build_institutional_flow",
]
