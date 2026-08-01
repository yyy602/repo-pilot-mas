from pathlib import Path

import pytest

from repo_pilot_mas.runtime import PathGuard, PathSecurityError


def test_path_guard_accepts_relative_file(sample_repo: Path) -> None:
    guard = PathGuard(sample_repo)

    resolved = guard.resolve("src/math_utils.py")

    assert resolved == sample_repo / "src" / "math_utils.py"


def test_path_guard_rejects_absolute_and_traversal(sample_repo: Path) -> None:
    guard = PathGuard(sample_repo)

    with pytest.raises(PathSecurityError):
        guard.resolve("/etc/passwd")
    with pytest.raises(PathSecurityError):
        guard.resolve("../../etc/passwd", must_exist=False)
    with pytest.raises(PathSecurityError):
        guard.resolve(".git/config")


def test_path_guard_rejects_symlink_escape(sample_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (sample_repo / "escape").symlink_to(outside)
    guard = PathGuard(sample_repo)

    with pytest.raises(PathSecurityError):
        guard.resolve("escape")
