"""Research-only contracts for the future GIGBAY Trader Brain.

This package represents beliefs and proposals.  It cannot place orders, allocate
real capital, enable leverage, or mutate the safety constitution.  The SENSE layer
only admits evidence for an explicit point in time; it calls no provider.
The THINK layer turns that admitted evidence into competing, falsifiable beliefs; it
re-proves the SENSE boundary itself instead of trusting it, and fails closed.
The PROVE layer grades a proposal against a predeclared, complete, embargoed and
cost-aware walk-forward record; it is an offline harness with no persistence,
provider, execution or risk integration, and it fails closed.
The LEARN layer assesses regime drift, compares a champion with a challenger on
proof alone, and records evidence-backed, exactly reversible retirements; it holds
no promotion, deployment, sizing or execution authority, and it fails closed.
"""

from .contracts import (
                        Assertion,
                        Belief,
                        BrainProposal,
                        Constitution,
                        Evidence,
                        EvidenceQuality,
                        Hypothesis,
                        InvalidationCondition,
                        ProposalAction,
                        Scenario,
                        Stance,
)
from .learn import (
                        ComparisonFailure,
                        ComparisonInputs,
                        ComparisonPreference,
                        ComparisonResult,
                        DriftFailure,
                        DriftInputs,
                        DriftResult,
                        ModelRecord,
                        ModelRole,
                        ProofSummary,
                        ReinstatementInputs,
                        ReinstatementResult,
                        RetirementGround,
                        RetirementInputs,
                        RetirementResult,
                        TransitionFailure,
                        evaluate_comparison,
                        evaluate_drift,
                        evaluate_reinstatement,
                        evaluate_retirement,
)
from .prove import (
                        AggregateMetrics,
                        EvaluationWindow,
                        ExpectedOutcome,
                        OutcomeManifest,
                        OutcomeObservation,
                        ProveFailure,
                        ProveInputs,
                        ProveResult,
                        WindowMetrics,
                        WindowRole,
                        evaluate_prove,
                        proposal_identity,
)
from .sense import ContradictionGroup, RejectedEvidence, SenseFailure, SenseResult, evaluate_sense
from .think import Judgement, ThinkFailure, ThinkResult, evaluate_think

__all__ = ["AggregateMetrics", "Assertion", "Belief", "BrainProposal", "ComparisonFailure",
           "ComparisonInputs", "ComparisonPreference", "ComparisonResult", "Constitution",
           "ContradictionGroup", "DriftFailure", "DriftInputs", "DriftResult", "EvaluationWindow",
           "Evidence", "EvidenceQuality", "ExpectedOutcome", "Hypothesis", "InvalidationCondition",
           "Judgement", "ModelRecord", "ModelRole", "OutcomeManifest", "OutcomeObservation",
           "ProofSummary", "ProposalAction", "ProveFailure", "ProveInputs", "ProveResult",
           "ReinstatementInputs", "ReinstatementResult", "RejectedEvidence", "RetirementGround",
           "RetirementInputs", "RetirementResult", "Scenario", "SenseFailure", "SenseResult",
           "Stance", "ThinkFailure", "ThinkResult", "TransitionFailure", "WindowMetrics",
           "WindowRole", "evaluate_comparison", "evaluate_drift", "evaluate_prove",
           "evaluate_reinstatement", "evaluate_retirement", "evaluate_sense", "evaluate_think",
           "proposal_identity"]
