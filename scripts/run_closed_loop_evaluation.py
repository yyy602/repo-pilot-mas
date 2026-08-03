"""Run final RepoPilot-MAS closed-loop evaluation and write reproducible reports."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from repo_pilot_mas.evaluation import aggregate_system_results, load_evaluation_suite


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="data/quixbugs/closed_loop_suite.json")
    parser.add_argument("--report-root", default="reports/quixbugs_closed_loop")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    suite = load_evaluation_suite(args.suite)
    root = Path(args.report_root)
    root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "dataset": suite.dataset,
        "task_count": len(suite.test_tasks),
        "tasks": [task.task_id for task in suite.test_tasks],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "dry_run" if args.dry_run else "runtime",
    }
    save_json(root / "manifest.json", manifest)

    if args.dry_run:
        results = [
            {
                "system_id": "closed_loop_dynamic",
                "task_id": task.task_id,
                "status": "pending",
                "validation": {},
                "usage": {},
                "mechanism": {"execution_path": "dynamic"},
            }
            for task in suite.test_tasks
        ]
        save_json(root / "pending_tasks.json", {"tasks": results})
        return 0

    raise RuntimeError(
        "Runtime evaluation requires configured model/runtime execution. "
        "Use the generated suite with the project evaluation entrypoint."
    )


if __name__ == "__main__":
    raise SystemExit(main())
