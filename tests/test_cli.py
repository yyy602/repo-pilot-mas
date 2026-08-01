from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_run_task_cli_completes_one_fake_model_task(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    repository = tmp_path / "repository"
    (repository / "tests").mkdir(parents=True)
    (repository / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "task_id": "cli-task",
                "repository_path": str(repository),
                "issue": "add returns the wrong result",
                "failing_tests": ["tests/test_calculator.py"],
                "protected_paths": ["tests"],
                "max_runtime_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    patch = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
"""
    responses_path = tmp_path / "responses.json"
    responses_path.write_text(
        json.dumps(
            [
                {
                    "thought_summary": "apply fix",
                    "action": {
                        "type": "tool",
                        "tool_name": "apply_patch",
                        "arguments": {"patch_text": patch},
                    },
                },
                {
                    "thought_summary": "finish",
                    "action": {"type": "final", "status": "success", "reason": "fixed"},
                },
            ]
        ),
        encoding="utf-8",
    )
    model_config = tmp_path / "model.yaml"
    model_config.write_text(
        "model:\n"
        "  provider: fake\n"
        f"  responses_path: {responses_path}\n"
        "generation:\n"
        "  max_retries: 1\n",
        encoding="utf-8",
    )
    runtime_config = tmp_path / "runtime.yaml"
    runtime_config.write_text(
        "single_agent:\n  max_steps: 3\n  max_runtime_seconds: 30\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "run_task.py"),
            "--task",
            str(task_path),
            "--model-config",
            str(model_config),
            "--runtime-config",
            str(runtime_config),
            "--report-root",
            str(tmp_path / "reports"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "succeeded"
    assert Path(report["trace_path"]).is_file()
    assert Path(report["report_path"]).is_file()
