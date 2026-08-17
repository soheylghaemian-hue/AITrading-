from datetime import UTC, datetime, timedelta

import pytest

from atp.brain import Belief, BrainProposal, Constitution, Evidence, Scenario
from atp.brain.contracts import EvidenceQuality, ProposalAction

NOW = datetime(2026, 8, 18, tzinfo=UTC)


def test_temporal_evidence_rejects_future_leakage():
    with pytest.raises(ValueError):
        Evidence("e1", "source", NOW, NOW, NOW - timedelta(seconds=1),
                 EvidenceQuality.VERIFIED, "sha256:x")


def test_scenarios_are_falsifiable_and_proposals_are_stable():
    scenario = Scenario("s1", "expectations exceed reality", 0.6, ("guidance improves",))
    proposal = BrainProposal("p1", NOW, ProposalAction.STUDY, "study surprise", (scenario,),
                             ("point-in-time expectations",), "HIGH")
    assert proposal.checksum() == proposal.checksum()
    with pytest.raises(ValueError):
        Scenario("bad", "story only", 0.8, ())


def test_research_constitution_cannot_enable_trading_or_leverage():
    Constitution()
    with pytest.raises(ValueError):
        Constitution(leverage_enabled=True)
    with pytest.raises(ValueError):
        Constitution(autonomous_execution=True)


def test_belief_score_is_bounded():
    Belief("claim", 0.5, ("e1",), (), NOW + timedelta(days=1))
    with pytest.raises(ValueError):
        Belief("claim", 1.1, (), (), NOW)
