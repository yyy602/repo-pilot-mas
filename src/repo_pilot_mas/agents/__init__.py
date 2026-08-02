"""Agent implementations."""

from repo_pilot_mas.agents.diagnostician import DiagnosticianAgent
from repo_pilot_mas.agents.investigator import InvestigatorAgent
from repo_pilot_mas.agents.patch_agent import PatchAgent
from repo_pilot_mas.agents.reviewer import ReviewerAgent
from repo_pilot_mas.agents.single_agent import SingleAgent
from repo_pilot_mas.agents.supervisor import (
    DYNAMIC_SUPERVISOR_PROMPT,
    ScriptedSupervisor,
    SupervisorAgent,
    SupervisorAgentError,
    SupervisorOutcome,
)
from repo_pilot_mas.agents.worker_common import WorkerAgentError
from repo_pilot_mas.agents.worker_pool import WorkerPool

__all__ = [
    "DYNAMIC_SUPERVISOR_PROMPT",
    "DiagnosticianAgent",
    "InvestigatorAgent",
    "PatchAgent",
    "ReviewerAgent",
    "ScriptedSupervisor",
    "SingleAgent",
    "SupervisorAgent",
    "SupervisorAgentError",
    "SupervisorOutcome",
    "WorkerAgentError",
    "WorkerPool",
]
