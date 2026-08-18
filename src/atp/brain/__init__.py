"""Research-only contracts for the future GIGBAY Trader Brain.

This package represents beliefs and proposals.  It cannot place orders, allocate
real capital, enable leverage, or mutate the safety constitution.  The SENSE layer
only admits evidence for an explicit point in time; it calls no provider.
"""

from .contracts import (Assertion, Belief, BrainProposal, Constitution, Evidence, EvidenceQuality,
                        ProposalAction, Scenario, Stance)
from .sense import ContradictionGroup, RejectedEvidence, SenseFailure, SenseResult, evaluate_sense

__all__ = ["Assertion", "Belief", "BrainProposal", "Constitution", "ContradictionGroup", "Evidence",
           "EvidenceQuality", "ProposalAction", "RejectedEvidence", "Scenario", "SenseFailure",
           "SenseResult", "Stance", "evaluate_sense"]
