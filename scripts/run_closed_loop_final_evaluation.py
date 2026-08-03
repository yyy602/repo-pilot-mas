"""Run the final closed-loop evaluation on the frozen QuixBugs test split."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import load_evaluation_suite


def _save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


async def run(args: argparse.Namespace) -> int:
    config = load_yaml(args.config)
    suite = load_evaluation_suite(args.suite)

    if args.split == "test":
        tasks = suite.test_tasks
    else:
        tasks = suite.development_tasks

    root = Path(args.report_root).resolve() / args.run_id
    root.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schema_version": 1,
        "protocol": config["protocol"],
        "dataset": suite.dataset,
        "suite_id": suite.suite_id,
        "split": args.split,
        "task_count": len(tasks),
        "tasks": [task.task_id for task in tasks],
        "repository_commit": _git_commit(),
        "seed": suite.seed,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "runtime",
    }

    _save_json(root / "manifest.json", manifest)

    # Final Runtime execution is intentionally delegated to the existing Phase6
    # execution engine after the protocol is frozen. This wrapper only fixes
    # reproducibility metadata and report layout.
    if args.dry_run:
        _save_json(
            root / "pending_tasks.json",
            {
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "status": "pending",
                    }
                    for task in tasks
                ]
            },
        )
        return 0

    raise RuntimeError(
        "Final runtime execution adapter is not configured. "
        "Use run_phase6_evaluation.py with the frozen closed-loop protocol."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/closed_loop_evaluation.yaml",
    )
    parser.add_argument(
        "--suite",
        default="data/quixbugs/phase6_suite.json",
    )
    parser.add_argument(
        "--split",
        choices=("development", "test"),
        default="test",
    )
    parser.add_argument(
        "--report-root",
        default="reports/closed_loop/evaluation",
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
