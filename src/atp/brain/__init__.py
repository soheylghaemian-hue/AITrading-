"""Research-only contracts for the future GIGBAY Trader Brain.

This package represents beliefs and proposals.  It cannot place orders, allocate
real capital, enable leverage, or mutate the safety constitution.  The SENSE layer
only admits evidence for an explicit point in time; it calls no provider.
The THINK layer turns that admitted evidence into competing, falsifiable beliefs; it
re-proves the SENSE boundary itself instead of trusting it, and fails closed.
"""

from .contracts import (Assertion, Belief, BrainProposal, Constitution, Evidence, EvidenceQuality,
                        Hypothesis, InvalidationCondition, ProposalAction, Scenario, Stance)
from .sense import ContradictionGroup, RejectedEvidence, SenseFailure, SenseResult, evaluate_sense
from .think import Judgement, ThinkFailure, ThinkResult, evaluate_think

__all__ = ["Assertion", "Belief", "BrainProposal", "Constitution", "ContradictionGroup", "Evidence",
           "EvidenceQuality", "Hypothesis", "InvalidationCondition", "Judgement", "ProposalAction",
           "RejectedEvidence", "Scenario", "SenseFailure", "SenseResult", "Stance", "ThinkFailure",
           "ThinkResult", "evaluate_sense", "evaluate_think"]
