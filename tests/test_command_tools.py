from __future__ import annotations

import sys
import time
from pathlib import Path

from repo_pilot_mas.tools import run_tests, static_check


def test_run_tests_passes(sample_repo: Path) -> None:
    result = run_tests(sample_repo, timeout_seconds=10)

    assert result.ok
    assert result.exit_code == 0
    assert result.data["passed"] is True
    assert result.command is not None
    assert Path(result.command[0]).resolve() == Path(sys.executable).resolve()


def test_run_tests_normalizes_bare_pytest_to_current_interpreter(sample_repo: Path) -> None:
    result = run_tests(sample_repo, command=("pytest", "-q"), timeout_seconds=10)

    assert result.ok
    assert result.command is not None
    assert Path(result.command[0]).resolve() == Path(sys.executable).resolve()
    assert result.command[1:3] == ("-m", "pytest")


def test_run_tests_reports_test_failure(tmp_path: Path) -> None:
    root = tmp_path / "failing"
    root.mkdir()
    (root / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    result = run_tests(root, timeout_seconds=10)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TEST_FAILED"
    assert result.exit_code == 1


def test_run_tests_rejects_arbitrary_python(sample_repo: Path) -> None:
    result = run_tests(sample_repo, command=(sys.executable, "-c", "print('unsafe')"))

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "RUN_TESTS_ERROR"


def test_run_tests_rejects_spoofed_python(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    fake_python = tmp_path / "python"
    fake_python.symlink_to("/bin/true")

    result = run_tests(
        sample_repo,
        command=(str(fake_python), "-m", "pytest", "-q"),
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "RUN_TESTS_ERROR"
    assert "not trusted" in result.error.message


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


def test_run_tests_bounds_stdout_and_stderr(tmp_path: Path) -> None:
    root = tmp_path / "noisy"
    root.mkdir()
    (root / "test_noisy.py").write_text(
        "import sys\n\n"
        "def test_noisy():\n"
        "    print('STDOUT-BEGIN-' + 'x' * 5000 + '-STDOUT-END')\n"
        "    print('STDERR-BEGIN-' + 'y' * 5000 + '-STDERR-END', file=sys.stderr)\n",
        encoding="utf-8",
    )

    result = run_tests(
        root,
        command=(sys.executable, "-m", "pytest", "-q", "-s"),
        timeout_seconds=10,
        max_output_chars=500,
    )

    assert result.ok
    assert result.truncated
    assert len(result.stdout) <= 500
    assert len(result.stderr) <= 500
    assert "<output truncated>" in result.stdout
    assert "<output truncated>" in result.stderr


def test_run_tests_timeout_terminates_child_process_group(tmp_path: Path) -> None:
    root = tmp_path / "process_group"
    root.mkdir()
    marker = tmp_path / "child-survived.txt"
    child_code = f"import time; time.sleep(0.5); open({str(marker)!r}, 'w').write('bad')"
    (root / "test_child.py").write_text(
        "import subprocess\nimport sys\nimport time\n\n"
        "def test_child():\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "    time.sleep(5)\n",
        encoding="utf-8",
    )

    result = run_tests(root, timeout_seconds=0.2)
    time.sleep(0.6)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TEST_TIMEOUT"
    assert not marker.exists()


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
