"""Deterministic input contracts for closed-loop worker dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class AgentContractViolation(ValueError):
    """Raised when a worker task is created with insufficient artifacts."""

    code = "AGENT_INPUT_CONTRACT_VIOLATION"


def validate_agent_input_contract(
    agent_type: str,
    mode: str,
    artifacts: Iterable[Mapping[str, object]],
) -> None:
    """Validate worker inputs before dispatch.

    This prevents deterministic dependency mistakes from reaching WorkerPool.
    The worker should not be used to discover missing upstream artifacts.
    """

    items = list(artifacts)
    types = {
        str(item.get("artifact_type", ""))
        for item in items
    }

    def require(*required: str) -> None:
        missing = [item for item in required if item not in types]
        if missing:
            raise AgentContractViolation(
                f"{agent_type}/{mode} missing required artifacts: {missing}"
            )

    if agent_type == "DiagnosticianAgent":
        require("evidence")
    elif agent_type == "ReviewerAgent":
        if mode in {
            "root_cause_recommendation",
            "hypothesis_comparison",
        }:
            require("evidence", "hypothesis")
        elif mode == "patch_review":
            require("patch_candidate", "hypothesis")
    elif agent_type == "PatchAgent":
        require("hypothesis")
    elif agent_type == "ValidationExecutor":
        require("patch_candidate")
