from __future__ import annotations

import sys
from pathlib import Path

from repo_pilot_mas.tools import run_tests, static_check


def test_run_tests_passes(sample_repo: Path) -> None:
    result = run_tests(sample_repo, timeout_seconds=10)

    assert result.ok
    assert result.exit_code == 0
    assert result.data["passed"] is True


def test_run_tests_rejects_arbitrary_python(sample_repo: Path) -> None:
    result = run_tests(sample_repo, command=(sys.executable, "-c", "print('unsafe')"))

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "RUN_TESTS_ERROR"


def test_run_tests_times_out(tmp_path: Path) -> None:
    root = tmp_path / "slow"
    root.mkdir()
    (root / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(2)\n",
        encoding="utf-8",
    )

    result = run_tests(root, timeout_seconds=0.1)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TEST_TIMEOUT"


def test_static_check_detects_syntax_error(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = static_check(root, run_ruff=False)

    assert not result.ok
    assert result.data["syntax_errors"][0]["path"] == "broken.py"


def test_static_check_accepts_valid_python(sample_repo: Path) -> None:
    result = static_check(sample_repo, run_ruff=False)

    assert result.ok
    assert result.data["checked_files"] == 2
