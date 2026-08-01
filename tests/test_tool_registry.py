from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.schemas import TaskSpec
from repo_pilot_mas.tools import build_workspace_tool_registry


def test_workspace_registry_exposes_exactly_nine_structured_tools(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    task = TaskSpec(task_id="registry", repository_path=sample_repo, issue="Inspect bug")
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("registry")
    registry = build_workspace_tool_registry(task, workspace)

    assert len(registry.names) == 9
    assert set(registry.names) == {
        "apply_patch",
        "collect_diff",
        "find_references",
        "inspect_code",
        "list_files",
        "rollback_workspace",
        "run_tests",
        "search_code",
        "static_check",
    }
    assert all(definition["parameters"]["type"] == "object" for definition in registry.definitions_for_model())


def test_registry_rejects_unknown_tool_and_unregistered_arguments(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    task = TaskSpec(task_id="registry", repository_path=sample_repo, issue="Inspect bug")
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("registry")
    registry = build_workspace_tool_registry(task, workspace)

    unknown = registry.invoke("shell", {"command": "rm -rf /"})
    changed_command = registry.invoke(
        "run_tests",
        {"scope": "full", "command": ["/bin/true"]},
    )

    assert not unknown.ok
    assert unknown.error is not None
    assert unknown.error.code == "TOOL_NOT_REGISTERED"
    assert not changed_command.ok
    assert changed_command.error is not None
    assert changed_command.error.code == "TOOL_ARGUMENT_ERROR"


def test_registry_binds_protected_paths_outside_agent_arguments(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    task = TaskSpec(
        task_id="registry",
        repository_path=sample_repo,
        issue="Inspect bug",
        protected_paths=("tests",),
    )
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("registry")
    registry = build_workspace_tool_registry(task, workspace)
    patch = """--- a/tests/test_math.py
+++ b/tests/test_math.py
@@ -3,3 +3,3 @@
 def test_add() -> None:
-    assert add(2, 3) == 5
+    assert add(2, 3) == 999
"""

    result = registry.invoke("apply_patch", {"patch_text": patch})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PROTECTED_PATH_VIOLATION"
