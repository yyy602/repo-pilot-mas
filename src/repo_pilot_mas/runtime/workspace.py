"""Isolated workspace creation and restoration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from repo_pilot_mas.runtime.path_guard import PathGuard

_IGNORE_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    ".venv",
    "venv",
}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Workspace:
    task_id: str
    workspace_id: str
    root: Path
    baseline_root: Path
    container_root: Path
    manager_root: Path
    baseline_digest: str

    def guard(self) -> PathGuard:
        return PathGuard(self.root)


class WorkspaceManager:
    """Create and dispose one independent working copy per candidate patch."""

    def __init__(self, source_root: str | Path, workspace_root: str | Path) -> None:
        self.source_root = Path(source_root).expanduser().resolve(strict=True)
        if not self.source_root.is_dir():
            raise ValueError("source_root must be a directory")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        if _is_within(self.workspace_root, self.source_root):
            raise ValueError("workspace_root must not be inside source_root")
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def create(self, task_id: str, workspace_id: str | None = None) -> Workspace:
        task_id = _validate_id(task_id, "task_id")
        workspace_id = _validate_id(workspace_id or uuid4().hex[:12], "workspace_id")
        container_root = self.workspace_root / task_id / workspace_id
        if container_root.exists():
            raise FileExistsError(f"workspace already exists: {container_root}")

        baseline_root = container_root / "baseline"
        worktree_root = container_root / "worktree"
        try:
            container_root.mkdir(parents=True)
            shutil.copytree(
                self.source_root,
                baseline_root,
                ignore=_ignore_entries,
                symlinks=True,
            )
            shutil.copytree(baseline_root, worktree_root, symlinks=True)
            _make_tree_writable(worktree_root)
            baseline_digest = _tree_digest(baseline_root)
            metadata = {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "source_root": str(self.source_root),
                "baseline_digest": baseline_digest,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (container_root / "workspace.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _make_tree_read_only(baseline_root)
            workspace = Workspace(
                task_id=task_id,
                workspace_id=workspace_id,
                root=worktree_root,
                baseline_root=baseline_root,
                container_root=container_root,
                manager_root=self.workspace_root,
                baseline_digest=baseline_digest,
            )
            validate_workspace(workspace)
            return workspace
        except Exception:
            if container_root.is_dir() and not container_root.is_symlink():
                if baseline_root.is_dir() and not baseline_root.is_symlink():
                    _make_tree_writable(baseline_root)
                shutil.rmtree(container_root)
            raise

    def open(self, task_id: str, workspace_id: str) -> Workspace:
        task_id = _validate_id(task_id, "task_id")
        workspace_id = _validate_id(workspace_id, "workspace_id")
        container_root = self.workspace_root / task_id / workspace_id
        _require_real_directory(container_root, "workspace container")
        metadata_path = container_root / "workspace.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ValueError("workspace metadata is missing or unsafe")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        workspace = Workspace(
            task_id=task_id,
            workspace_id=workspace_id,
            root=container_root / "worktree",
            baseline_root=container_root / "baseline",
            container_root=container_root,
            manager_root=self.workspace_root,
            baseline_digest=str(metadata.get("baseline_digest", "")),
        )
        validate_workspace(workspace)
        return workspace

    def delete(self, workspace: Workspace) -> None:
        if _absolute(workspace.manager_root) != self.workspace_root:
            raise ValueError("workspace belongs to another manager root")
        validate_workspace(workspace, require_worktree=False)
        _make_tree_writable(workspace.baseline_root)
        try:
            shutil.rmtree(workspace.container_root)
        except OSError:
            if workspace.baseline_root.is_dir() and not workspace.baseline_root.is_symlink():
                _make_tree_read_only(workspace.baseline_root)
            raise
        _remove_empty_directory(workspace.container_root.parent)

    def discard(self, task_id: str, workspace_id: str) -> bool:
        """Idempotently delete one managed workspace by identity."""

        task_id = _validate_id(task_id, "task_id")
        workspace_id = _validate_id(workspace_id, "workspace_id")
        container_root = self.workspace_root / task_id / workspace_id
        if not container_root.exists() and not container_root.is_symlink():
            _remove_empty_directory(container_root.parent)
            return False
        workspace = self.open(task_id, workspace_id)
        self.delete(workspace)
        return True

    def discard_task(self, task_id: str) -> tuple[str, ...]:
        """Delete every remaining managed workspace for one terminated task."""

        task_id = _validate_id(task_id, "task_id")
        task_root = self.workspace_root / task_id
        if not task_root.exists() and not task_root.is_symlink():
            return ()
        _require_real_directory(task_root, "workspace task directory")
        workspace_ids: list[str] = []
        for entry in sorted(task_root.iterdir(), key=lambda item: item.name):
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("workspace task directory contains an unsafe entry")
            workspace_id = _validate_id(entry.name, "workspace_id")
            self.discard(task_id, workspace_id)
            workspace_ids.append(workspace_id)
        _remove_empty_directory(task_root)
        return tuple(workspace_ids)


def validate_workspace(workspace: Workspace, *, require_worktree: bool = True) -> None:
    """Verify managed layout, metadata identity, symlink safety, and baseline integrity."""

    task_id = _validate_id(workspace.task_id, "task_id")
    workspace_id = _validate_id(workspace.workspace_id, "workspace_id")
    manager_root = _absolute(workspace.manager_root)
    _require_real_directory(manager_root, "workspace manager root")

    expected_container = manager_root / task_id / workspace_id
    expected_baseline = expected_container / "baseline"
    expected_worktree = expected_container / "worktree"
    if _absolute(workspace.container_root) != expected_container:
        raise ValueError("workspace container does not match the managed layout")
    if _absolute(workspace.baseline_root) != expected_baseline:
        raise ValueError("baseline root does not match the managed layout")
    if _absolute(workspace.root) != expected_worktree:
        raise ValueError("workspace root does not match the managed layout")

    _require_real_directory(expected_container.parent, "workspace task directory")
    _require_real_directory(expected_container, "workspace container")
    _require_real_directory(expected_baseline, "workspace baseline")
    if expected_worktree.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    if require_worktree:
        _require_real_directory(expected_worktree, "workspace root")
    elif expected_worktree.exists() and not expected_worktree.is_dir():
        raise ValueError("workspace root must be a directory when present")

    metadata_path = expected_container / "workspace.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ValueError("workspace metadata is missing or unsafe")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("task_id") != task_id or metadata.get("workspace_id") != workspace_id:
        raise ValueError("workspace metadata identity does not match")
    recorded_digest = metadata.get("baseline_digest")
    if not isinstance(recorded_digest, str) or not recorded_digest:
        raise ValueError("workspace baseline digest is missing")
    if workspace.baseline_digest != recorded_digest:
        raise ValueError("workspace baseline identity does not match")
    _require_tree_read_only(expected_baseline)
    if _tree_digest(expected_baseline) != recorded_digest:
        raise ValueError("workspace baseline integrity check failed")


def restore_workspace(workspace: Workspace) -> None:
    """Replace a verified worktree with a fresh writable copy of its baseline."""

    validate_workspace(workspace, require_worktree=False)
    if workspace.root.exists():
        shutil.rmtree(workspace.root)
    shutil.copytree(workspace.baseline_root, workspace.root, symlinks=True)
    _make_tree_writable(workspace.root)


def _ignore_entries(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in _IGNORE_NAMES or name.endswith((".pyc", ".pyo"))
    }


def _validate_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or not _ID_PATTERN.fullmatch(normalized) or ".." in normalized:
        raise ValueError(f"invalid {label}: {value!r}")
    return normalized


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    if path.resolve(strict=True) != path:
        raise ValueError(f"{label} contains a replaced symlink")


def _remove_empty_directory(path: Path) -> None:
    if not path.exists() or path.is_symlink() or not path.is_dir():
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode())
            continue
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
            continue
        if path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _make_tree_read_only(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(
            path.stat().st_mode
            & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        )


def _require_tree_read_only(root: Path) -> None:
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for path in (root, *root.rglob("*")):
        if not path.is_symlink() and path.stat().st_mode & write_bits:
            raise ValueError("workspace baseline is not read-only")


def _make_tree_writable(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
