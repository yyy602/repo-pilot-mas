from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager


def test_discard_workspace_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("x = 1\n", encoding="utf-8")

    manager = WorkspaceManager(source, tmp_path / "workspaces")
    workspace = manager.create("task", "patch-1")

    assert workspace.container_root.exists()

    assert manager.discard("task", "patch-1") is True
    assert not workspace.container_root.exists()

    assert manager.discard("task", "patch-1") is False


def test_discard_task_removes_failed_patch_candidates(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("x = 1\n", encoding="utf-8")

    manager = WorkspaceManager(source, tmp_path / "workspaces")
    manager.create("task", "patch-a")
    manager.create("task", "patch-b")

    removed = manager.discard_task("task")

    assert removed == ("patch-a", "patch-b")
    assert not (tmp_path / "workspaces" / "task").exists()
