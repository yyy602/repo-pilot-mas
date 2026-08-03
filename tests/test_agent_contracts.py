from __future__ import annotations

import pytest

from repo_pilot_mas.orchestration.agent_contracts import (
    AgentContractViolation,
    validate_agent_input_contract,
)


def test_reviewer_root_cause_requires_evidence_and_hypothesis() -> None:
    with pytest.raises(AgentContractViolation):
        validate_agent_input_contract(
            "ReviewerAgent",
            "root_cause_recommendation",
            [{"artifact_type": "hypothesis"}],
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
