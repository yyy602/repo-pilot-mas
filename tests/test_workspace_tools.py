from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.tools import apply_patch, collect_diff, rollback_workspace


def test_workspace_is_isolated_and_patch_round_trip(sample_repo: Path, tmp_path: Path) -> None:
    manager = WorkspaceManager(sample_repo, tmp_path / "workspaces")
    workspace = manager.create("task-1", "candidate-a")
    patch = """--- a/src/math_utils.py
+++ b/src/math_utils.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
-    return left + right
+    return int(left) + int(right)
 
"""

    applied = apply_patch(workspace.root, patch)
    diff = collect_diff(workspace)

    assert applied.ok
    assert "int(left)" in (workspace.root / "src" / "math_utils.py").read_text(encoding="utf-8")
    assert diff.ok
    assert diff.data["changed_count"] == 1
    assert "int(left)" in diff.data["diff"]
    assert "int(left)" not in (sample_repo / "src" / "math_utils.py").read_text(encoding="utf-8")

    rolled_back = rollback_workspace(workspace)
    clean = collect_diff(workspace)

    assert rolled_back.ok
    assert clean.ok
    assert clean.data["is_clean"] is True


def test_patch_check_failure_does_not_mutate_workspace(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-2")
    before = (workspace.root / "src" / "math_utils.py").read_text(encoding="utf-8")
    invalid_patch = """--- a/src/math_utils.py
+++ b/src/math_utils.py
@@ -1,2 +1,2 @@
-definitely not present
+replacement
"""

    result = apply_patch(workspace.root, invalid_patch)

    assert not result.ok
    assert (workspace.root / "src" / "math_utils.py").read_text(encoding="utf-8") == before


def test_patch_rejects_path_traversal(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-3")
    patch = """--- /dev/null
+++ b/../../escape.txt
@@ -0,0 +1 @@
+owned
"""

    result = apply_patch(workspace.root, patch)

    assert not result.ok
    assert not (tmp_path / "escape.txt").exists()
