"""运行 Phase 6 正式 Development 前的可复现代码与冻结检查。"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.run_closed_loop_final_evaluation import (
    _repository_fingerprint,
    _runtime_tree_is_clean,
    pre_freeze_record_gates,
)

_CHECK_COMMANDS = (
    (sys.executable, "-m", "pytest"),
    (sys.executable, "-m", "ruff", "check", "."),
    (sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"),
    ("git", "diff", "--check"),
)
_RUNTIME_STATUS_COMMAND = (
    "git",
    "status",
    "--short",
    "--untracked-files=all",
    "--",
    "pyproject.toml",
    "configs",
    "data",
    "scripts",
    "src",
    "tests",
)


def _run_check(command: tuple[str, ...], root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    return {
        "command": shlex.join(command),
        "exit_code": completed.returncode,
        "result": "passed" if completed.returncode == 0 else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/closed_loop/verification/pre_freeze_checks.json",
    )
    args = parser.parse_args()

    root = Path.cwd().resolve()
    fingerprint = _repository_fingerprint(root)
    checks = [_run_check(command, root) for command in _CHECK_COMMANDS]
    runtime_tree_clean = _runtime_tree_is_clean(root)
    checks.append(
        {
            "command": shlex.join(_RUNTIME_STATUS_COMMAND),
            "exit_code": 0,
            "result": "clean" if runtime_tree_clean else "dirty",
        }
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": 6,
        "branch": subprocess.run(
            ("git", "branch", "--show-current"),
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": sys.executable,
        "repository_fingerprint": fingerprint,
        "passed": all(item["exit_code"] == 0 for item in checks)
        and runtime_tree_clean,
        "checks": checks,
    }
    payload["verification_gates"] = pre_freeze_record_gates(
        payload,
        fingerprint,
        runtime_tree_clean=runtime_tree_clean,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": payload["passed"],
                "failed_gates": [
                    name
                    for name, passed in payload["verification_gates"].items()
                    if not passed
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
