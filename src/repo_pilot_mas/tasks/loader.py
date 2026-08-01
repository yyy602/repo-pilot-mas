"""Load validated TaskSpec manifests from disk."""

from __future__ import annotations

import json
from pathlib import Path

from repo_pilot_mas.schemas.task import TaskSpec


def load_task(path: str | Path) -> TaskSpec:
    manifest_path = Path(path).expanduser().resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError("task manifest must be a file")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("task manifest must contain one JSON object")
    repository_path = Path(payload["repository_path"]).expanduser()
    if not repository_path.is_absolute():
        repository_path = manifest_path.parent / repository_path
    repository_path = repository_path.resolve(strict=True)
    if not repository_path.is_dir():
        raise ValueError("task repository_path must be a directory")
    payload["repository_path"] = str(repository_path)
    return TaskSpec.from_dict(payload)


def load_tasks(directory: str | Path) -> tuple[TaskSpec, ...]:
    task_directory = Path(directory).expanduser().resolve(strict=True)
    if not task_directory.is_dir():
        raise ValueError("task directory must be a directory")
    tasks = tuple(load_task(path) for path in sorted(task_directory.glob("*.json")))
    if not tasks:
        raise ValueError("task directory does not contain JSON manifests")
    task_ids = [task.task_id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task directory contains duplicate task_id values")
    return tasks
