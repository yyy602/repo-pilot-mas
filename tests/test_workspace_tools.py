import shutil
import stat
from pathlib import Path

import pytest

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


def test_patch_supports_add_and_delete(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-4")
    patch = """--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1 @@
+VALUE = 42
--- a/README.md
+++ /dev/null
@@ -1 +0,0 @@
-sample repository
"""

    result = apply_patch(workspace.root, patch)
    diff = collect_diff(workspace)

    assert result.ok
    assert (workspace.root / "src" / "new_module.py").is_file()
    assert not (workspace.root / "README.md").exists()
    assert diff.ok
    statuses = {change["path"]: change["status"] for change in diff.data["files"]}
    assert statuses["src/new_module.py"] == "added"
    assert statuses["README.md"] == "deleted"


def test_patch_rejects_binary_patch(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-5")
    patch = """diff --git a/binary.dat b/binary.dat
GIT binary patch
literal 1
abc
"""

    result = apply_patch(workspace.root, patch)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "APPLY_PATCH_ERROR"


def test_collect_diff_reports_binary_change(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-binary")
    (workspace.root / "binary.dat").write_bytes(b"changed\x00binary")

    result = collect_diff(workspace)

    assert result.ok
    binary_change = next(
        change for change in result.data["files"] if change["path"] == "binary.dat"
    )
    assert binary_change["binary"] is True
    assert "Binary files" in result.data["diff"]


def test_candidate_workspaces_are_independent(sample_repo: Path, tmp_path: Path) -> None:
    manager = WorkspaceManager(sample_repo, tmp_path / "workspaces")
    candidate_a = manager.create("task-6", "candidate-a")
    candidate_b = manager.create("task-6", "candidate-b")

    (candidate_a.root / "src" / "math_utils.py").write_text("candidate a\n", encoding="utf-8")

    assert "candidate a" not in (candidate_b.root / "src" / "math_utils.py").read_text(
        encoding="utf-8"
    )
    assert "candidate a" not in (sample_repo / "src" / "math_utils.py").read_text(
        encoding="utf-8"
    )


def test_modified_baseline_is_rejected_as_rollback_source(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-7")
    baseline_file = workspace.baseline_root / "src" / "math_utils.py"
    original_mode = baseline_file.stat().st_mode
    assert not original_mode & stat.S_IWUSR
    baseline_file.chmod(original_mode | stat.S_IWUSR)
    baseline_file.write_text("poisoned baseline\n", encoding="utf-8")
    baseline_file.chmod(original_mode)
    worktree_file = workspace.root / "src" / "math_utils.py"
    worktree_file.write_text("candidate content\n", encoding="utf-8")

    result = rollback_workspace(workspace)

    assert not result.ok
    assert result.error is not None
    assert "integrity" in result.error.message
    assert worktree_file.read_text(encoding="utf-8") == "candidate content\n"


def test_writable_baseline_is_rejected_as_rollback_source(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-writable")
    baseline_file = workspace.baseline_root / "src" / "math_utils.py"
    baseline_file.chmod(baseline_file.stat().st_mode | stat.S_IWUSR)

    result = rollback_workspace(workspace)

    assert not result.ok
    assert result.error is not None
    assert "not read-only" in result.error.message


def test_workspace_symlink_replacement_is_rejected(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(sample_repo, tmp_path / "workspaces")
    workspace = manager.create("task-8")
    victim = tmp_path / "victim"
    victim.mkdir()
    victim_file = victim / "keep.txt"
    victim_file.write_text("keep", encoding="utf-8")
    shutil.rmtree(workspace.root)
    workspace.root.symlink_to(victim, target_is_directory=True)

    rolled_back = rollback_workspace(workspace)
    diff = collect_diff(workspace)

    assert not rolled_back.ok
    assert not diff.ok
    with pytest.raises(ValueError):
        manager.delete(workspace)
    assert victim_file.read_text(encoding="utf-8") == "keep"


def test_patch_rejects_symbolic_link_creation(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-symlink")
    patch = """diff --git a/escape b/escape
new file mode 120000
index 0000000..1234567
--- /dev/null
+++ b/escape
@@ -0,0 +1 @@
+../../outside
\\ No newline at end of file
"""

    result = apply_patch(workspace.root, patch)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "APPLY_PATCH_ERROR"
    assert not (workspace.root / "escape").exists()


def test_missing_worktree_can_be_restored(sample_repo: Path, tmp_path: Path) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-restore")
    shutil.rmtree(workspace.root)

    result = rollback_workspace(workspace)

    assert result.ok
    assert (workspace.root / "src" / "math_utils.py").is_file()


def test_workspace_can_be_reopened_and_deleted(sample_repo: Path, tmp_path: Path) -> None:
    manager = WorkspaceManager(sample_repo, tmp_path / "workspaces")
    workspace = manager.create("task-delete", "candidate-a")

    reopened = manager.open("task-delete", "candidate-a")
    manager.delete(reopened)

    assert reopened.baseline_digest == workspace.baseline_digest
    assert not workspace.container_root.exists()


def test_protected_paths_are_checked_before_and_after_patch(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("task-9")
    test_file = workspace.root / "tests" / "test_math.py"
    before = test_file.read_text(encoding="utf-8")
    patch = """--- a/tests/test_math.py
+++ b/tests/test_math.py
@@ -3,3 +3,3 @@
 def test_add() -> None:
-    assert add(2, 3) == 5
+    assert add(2, 3) == 999
"""

    blocked_patch = apply_patch(workspace.root, patch, protected_paths=("tests",))
    assert not blocked_patch.ok
    assert blocked_patch.error is not None
    assert blocked_patch.error.code == "PROTECTED_PATH_VIOLATION"
    assert test_file.read_text(encoding="utf-8") == before

    test_file.write_text("direct candidate mutation\n", encoding="utf-8")
    blocked_diff = collect_diff(workspace, protected_paths=("tests",))
    assert not blocked_diff.ok
    assert blocked_diff.error is not None
    assert blocked_diff.error.code == "PROTECTED_PATH_VIOLATION"
    assert blocked_diff.data["protected_path_violations"] == ["tests/test_math.py"]


def test_collect_diff_bounds_text_and_large_file_content(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    (sample_repo / "many.txt").write_text("".join(f"old-{i}\n" for i in range(500)))
    manager = WorkspaceManager(sample_repo, tmp_path / "workspaces")
    workspace = manager.create("task-10")
    (workspace.root / "many.txt").write_text(
        "".join(f"new-{i}\n" for i in range(500)),
        encoding="utf-8",
    )

    bounded = collect_diff(workspace, max_diff_chars=500, max_file_bytes=100_000)
    assert bounded.ok
    assert bounded.truncated
    assert len(bounded.data["diff"]) <= 500

    (workspace.root / "large.txt").write_text("z" * 10_000, encoding="utf-8")
    large_file = collect_diff(workspace, max_diff_chars=500, max_file_bytes=100)
    assert large_file.ok
    assert "exceeds the diff size limit" in large_file.data["diff"]
    large_change = next(
        change for change in large_file.data["files"] if change["path"] == "large.txt"
    )
    assert large_change["new_size_bytes"] == 10_000
