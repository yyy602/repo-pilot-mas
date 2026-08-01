from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample_repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "src" / "math_utils.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n\n\n"
        "class Calculator:\n"
        "    def total(self, values: list[int]) -> int:\n"
        "        return sum(values)\n\n\n"
        "def use_add() -> int:\n"
        "    return add(1, 2)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_math.py").write_text(
        "from src.math_utils import add\n\n\n"
        "def test_add() -> None:\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("sample repository\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"abc\x00def")
    (root / ".git" / "config").write_text("secret\n", encoding="utf-8")
    return root
