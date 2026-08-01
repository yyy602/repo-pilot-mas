"""Task input schema for repository repair jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Validated task description consumed by later agent stages."""

    task_id: str
    repository_path: Path
    issue: str
    failing_tests: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ("python", "-m", "pytest", "-q")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        task_id = self.task_id.strip()
        issue = self.issue.strip()
        if not task_id:
            raise ValueError("task_id must not be empty")
        if any(char in task_id for char in ("/", "\\", "..")):
            raise ValueError("task_id must not contain path separators or '..'")
        if not issue:
            raise ValueError("issue must not be empty")
        if not self.test_command:
            raise ValueError("test_command must not be empty")

        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "issue", issue)
        object.__setattr__(self, "repository_path", Path(self.repository_path).expanduser())
        object.__setattr__(self, "failing_tests", tuple(self.failing_tests))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "test_command", tuple(self.test_command))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskSpec:
        return cls(
            task_id=str(value["task_id"]),
            repository_path=Path(value["repository_path"]),
            issue=str(value["issue"]),
            failing_tests=tuple(_as_strings(value.get("failing_tests", ()))),
            acceptance_criteria=tuple(_as_strings(value.get("acceptance_criteria", ()))),
            test_command=tuple(
                _as_strings(value.get("test_command", ("python", "-m", "pytest", "-q")))
            ),
            metadata=dict(value.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository_path": str(self.repository_path),
            "issue": self.issue,
            "failing_tests": list(self.failing_tests),
            "acceptance_criteria": list(self.acceptance_criteria),
            "test_command": list(self.test_command),
            "metadata": dict(self.metadata),
        }


def _as_strings(value: Sequence[Any]) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError("expected a sequence of strings, not one string")
    return [str(item) for item in value]
