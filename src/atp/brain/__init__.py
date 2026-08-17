"""Research-only contracts for the future GIGBAY Trader Brain.

This package represents beliefs and proposals.  It cannot place orders, allocate
real capital, enable leverage, or mutate the safety constitution.
"""

from .contracts import Belief, BrainProposal, Constitution, Evidence, Scenario

__all__ = ["Belief", "BrainProposal", "Constitution", "Evidence", "Scenario"]
