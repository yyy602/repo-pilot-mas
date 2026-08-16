from __future__ import annotations

from pathlib import Path

import pytest

from repo_pilot_mas.orchestration import OrchestrationEngine
from repo_pilot_mas.orchestration.agent_contracts import (
    AgentContractViolation,
    validate_agent_input_contract,
)
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)


def test_root_cause_review_requires_hypothesis_and_evidence() -> None:
    with pytest.raises(AgentContractViolation):
        validate_agent_input_contract(
            "ReviewerAgent",
            "root_cause_recommendation",
            [{"artifact_type": "hypothesis"}],
        )


def test_diagnosis_rejects_non_evidence_context_before_dispatch() -> None:
    with pytest.raises(AgentContractViolation, match="unsupported artifacts"):
        validate_agent_input_contract(
            "DiagnosticianAgent",
            "data_flow",
            [
                {"artifact_type": "evidence"},
                {"artifact_type": "hypothesis"},
                {"artifact_type": "review"},
            ],
        )


def test_reviewer_accepts_complete_root_cause_inputs() -> None:
    validate_agent_input_contract(
        "ReviewerAgent",
        "root_cause_recommendation",
        [
            {"artifact_type": "hypothesis"},
            {"artifact_type": "evidence"},
        ],
    )


def test_evidence_completion_requires_evidence_and_review_context() -> None:
    with pytest.raises(AgentContractViolation, match="missing required artifacts"):
        validate_agent_input_contract(
            "InvestigatorAgent",
            "evidence_completion",
            [{"artifact_type": "review"}],
        )

    validate_agent_input_contract(
        "InvestigatorAgent",
        "evidence_completion",
        [
            {"artifact_type": "evidence"},
            {"artifact_type": "review"},
        ],
    )

    with pytest.raises(AgentContractViolation, match="unsupported artifacts"):
        validate_agent_input_contract(
            "InvestigatorAgent",
            "evidence_completion",
            [
                {"artifact_type": "evidence"},
                {"artifact_type": "review"},
                {"artifact_type": "hypothesis"},
            ],
        )


def test_patch_requires_accepted_hypothesis_set() -> None:
    with pytest.raises(AgentContractViolation):
        validate_agent_input_contract(
            "PatchAgent",
            "minimal",
            [{"artifact_type": "hypothesis"}],
        )


def test_validation_requires_patch_candidate() -> None:
    with pytest.raises(AgentContractViolation):
        validate_agent_input_contract(
            "ValidationExecutor",
            "deterministic",
            [],
        )


def test_corrected_inputs_create_new_node(
    tmp_path: Path,
) -> None:
    engine = OrchestrationEngine(TaskSpec("contract-create", tmp_path, "review one root cause"))
    hypothesis = Artifact(
        "H1",
        ArtifactType.HYPOTHESIS,
        "N1",
        {"root_cause": "boundary mismatch"},
    )
    evidence = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N0",
        {"claim": "target failure reproduces the boundary mismatch"},
    )
    engine.add_artifact(hypothesis)
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("diagnosis")

    invalid = engine.apply_decision(
        SupervisorDecision(
            "review-without-evidence",
            DecisionAction.CREATE_TASK,
            "review the hypothesis",
            create_tasks=(
                CreateTaskRequest(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "root_cause_recommendation",
                    "independently review the hypothesis",
                    input_artifact_ids=(hypothesis.ref,),
                ),
            ),
        )
    )

    assert not invalid.ok
    assert invalid.code == "AGENT_INPUT_CONTRACT_VIOLATION"
    assert invalid.recoverable
    assert invalid.recommended_stage == "review"
    assert invalid.allowed_next_actions == ("CREATE_TASK",)
    assert engine.graph.nodes == ()

    corrected = engine.apply_decision(
        SupervisorDecision(
            "review-with-complete-inputs",
            DecisionAction.CREATE_TASK,
            "review the hypothesis with direct evidence",
            create_tasks=(
                CreateTaskRequest(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "root_cause_recommendation",
                    "independently review the hypothesis",
                    input_artifact_ids=(hypothesis.ref, evidence.ref),
                ),
            ),
        )
    )

    assert corrected.ok
    assert corrected.mutated_node_ids == ("N1",)
    assert engine.graph.get("N1").input_artifact_ids == (
        hypothesis.ref,
        evidence.ref,
    )
