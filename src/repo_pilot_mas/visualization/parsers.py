"""Read-only parsers for closed-loop evaluation artifacts.

阶段一（只读驾驶舱）的数据层：只消费磁盘上已有的 JSONL/JSON 产物，
不修改任何引擎行为。所有函数保持纯函数、可单测。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_EVALUATION_ROOT = Path("reports/closed_loop/evaluation")
_PHASE5_ACCEPTANCE_ROOT = Path("reports/phase5/acceptance")
_WEB_RUNS_ROOT = Path("reports/web_runs")


def read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON root must be an object: {path}")
    return dict(value)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, Mapping):
            events.append(dict(value))
    return events


@dataclass(frozen=True, slots=True)
class RunRef:
    """One run directory inside the evaluation reports root."""

    run_id: str
    root: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    demo: bool = False

    @property
    def split(self) -> str:
        return str(self.manifest.get("evaluation_split", "unknown"))

    @property
    def frozen(self) -> bool:
        value = self.manifest.get("frozen")
        if isinstance(value, bool):
            return value
        if self.acceptance is not None and isinstance(
            self.acceptance.get("frozen"), bool
        ):
            return bool(self.acceptance["frozen"])
        return bool(self.acceptance is not None and self.acceptance.get("passed"))

    @property
    def passed(self) -> bool | None:
        if self.acceptance is not None:
            return bool(self.acceptance.get("passed"))
        if self.manifest.get("evaluation_split") == "web":
            # web 运行无 acceptance 语义，用 solved 表示（避免仪表盘空值）
            return bool(self.solved)
        return None

    @property
    def solved(self) -> int | None:
        if self.summary is not None:
            return int(self.summary.get("solved", 0))
        if self.manifest.get("evaluation_split") == "web":
            return sum(
                1
                for task_id in self.task_ids
                if self._web_task_status(task_id) == "succeeded"
            )
        return None

    @property
    def task_count(self) -> int | None:
        if self.summary is not None:
            return int(self.summary.get("task_count", 0))
        if self.manifest.get("evaluation_split") == "web":
            return len(self.task_ids)
        return None

    def _web_task_status(self, task_id: str) -> str | None:
        result_path = self.root / "tasks" / task_id / "result.json"
        if not result_path.is_file():
            return None
        try:
            return str(read_json(result_path).get("status", "unknown"))
        except (OSError, ValueError, TypeError):
            return None

    @property
    def task_ids(self) -> tuple[str, ...]:
        if self.summary and "task_result_paths" in self.summary:
            return tuple(
                str(Path(item).parent.name)
                for item in self.summary["task_result_paths"]
            )
        if self.demo:
            return tuple(
                str(path.name[: -len("_trace.jsonl")])
                for path in sorted(self.root.glob("*_trace.jsonl"))
            )
        tasks = self.root / "tasks"
        if tasks.is_dir():
            # 运行中任务只有 trace.jsonl 没有 result.json，也要可见（实时查看）。
            return tuple(
                sorted(
                    path.name
                    for path in tasks.iterdir()
                    if path.is_dir()
                    and (
                        (path / "result.json").is_file()
                        or (path / "trace.jsonl").is_file()
                    )
                )
            )
        return ()

    @property
    def status(self) -> str | None:
        """web 运行暴露运行状态（queued/preparing/running/done/failed）。"""

        if self.manifest.get("evaluation_split") != "web":
            return None
        return str(self.manifest.get("status", "unknown"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "root": str(self.root),
            "split": self.split,
            "frozen": self.frozen,
            "passed": self.passed,
            "solved": self.solved,
            "task_count": self.task_count,
            "demo": self.demo,
            "task_ids": list(self.task_ids),
            "status": self.status,
            "started_at": self.manifest.get("started_at"),
            "finished_at": self.manifest.get("finished_at"),
            "repository_commit": self.manifest.get("repository_commit"),
            "supervisor_provider": self.manifest.get("supervisor_provider"),
        }


def _summary_sum(run: RunRef, field: str) -> int:
    if run.summary is None:
        return 0
    return int(run.summary.get(field, 0))


def discover_runs(
    evaluation_root: str | Path | None = None,
    *,
    demo_root: str | Path | None = None,
    web_root: str | Path | None = None,
) -> tuple[RunRef, ...]:
    """Scan evaluation runs, web runs, and Phase 5 mechanism demo traces."""

    root = Path(evaluation_root or _EVALUATION_ROOT).expanduser().resolve()
    runs: list[RunRef] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = read_json(manifest_path)
            except (OSError, ValueError, TypeError):
                continue
            acceptance = None
            summary = None
            acceptance_path = child / "acceptance.json"
            if acceptance_path.is_file():
                try:
                    acceptance = read_json(acceptance_path)
                except (OSError, ValueError, TypeError):
                    acceptance = None
            summary_path = child / "summary.json"
            if summary_path.is_file():
                try:
                    summary = read_json(summary_path)
                except (OSError, ValueError, TypeError):
                    summary = None
            runs.append(
                RunRef(
                    run_id=child.name,
                    root=child,
                    manifest=manifest,
                    acceptance=acceptance,
                    summary=summary,
                )
            )
    demo_path = Path(demo_root or _PHASE5_ACCEPTANCE_ROOT).expanduser().resolve()
    if demo_path.is_dir():
        for child in sorted(demo_path.iterdir()):
            if not child.is_dir():
                continue
            traces = sorted(child.glob("*_trace.jsonl"))
            if not traces:
                continue
            runs.append(
                RunRef(
                    run_id=f"phase5:{child.name}",
                    root=child,
                    manifest={"evaluation_split": "demo"},
                    demo=True,
                )
            )
    web_path = Path(web_root or _WEB_RUNS_ROOT).expanduser().resolve()
    if web_path.is_dir():
        for child in sorted(web_path.iterdir()):
            if not child.is_dir():
                continue
            run_manifest = child / "run.json"
            if not run_manifest.is_file():
                continue
            try:
                manifest = read_json(run_manifest)
            except (OSError, ValueError, TypeError):
                continue
            runs.append(
                RunRef(
                    run_id=child.name,
                    root=child,
                    manifest=manifest,
                )
            )
    return tuple(runs)


def load_run(
    run_id: str,
    evaluation_root: str | Path | None = None,
    *,
    web_root: str | Path | None = None,
) -> RunRef:
    for run in discover_runs(evaluation_root, web_root=web_root):
        if run.run_id == run_id:
            return run
    raise KeyError(f"unknown run: {run_id}")


@dataclass(frozen=True, slots=True)
class TaskView:
    """One task result plus its raw event stream."""

    run_id: str
    task_id: str
    result: dict[str, Any] | None
    events: tuple[dict[str, Any], ...]
    trace_path: str | None = None

    @property
    def status(self) -> str:
        if self.result is None:
            return "demo"
        return str(self.result.get("status", "unknown"))

    @property
    def termination(self) -> dict[str, Any]:
        if self.result is None:
            return {}
        return dict(self.result.get("termination", {}))

    @property
    def nodes(self) -> tuple[dict[str, Any], ...]:
        if self.result is None:
            return _nodes_from_events(self.events)
        return tuple(self.result.get("nodes", ()))

    @property
    def artifacts(self) -> tuple[dict[str, Any], ...]:
        if self.result is None:
            return _artifacts_from_events(self.events)
        return tuple(self.result.get("artifacts", ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "termination": self.termination,
            "workflow_stage": (
                self.result.get("workflow_stage") if self.result else None
            ),
            "nodes": list(self.nodes),
            "artifacts": list(self.artifacts),
            "event_count": len(self.events),
            "trace_path": self.trace_path,
            "validation": (self.result or {}).get("validation"),
            "usage": (self.result or {}).get("usage"),
            "mechanism": (self.result or {}).get("mechanism"),
            "closed_loop": (self.result or {}).get("closed_loop"),
            "hypothesis_resolution": (self.result or {}).get(
                "hypothesis_resolution"
            ),
            "source_integrity": (self.result or {}).get("source_integrity"),
        }


def load_task(
    run_id: str,
    task_id: str,
    evaluation_root: str | Path | None = None,
    *,
    web_root: str | Path | None = None,
) -> TaskView:
    run = load_run(run_id, evaluation_root, web_root=web_root)
    if run.demo:
        return _load_demo_task(run, task_id)
    task_root = run.root / "tasks" / task_id
    result_path = task_root / "result.json"
    trace_path = task_root / "trace.jsonl"
    result = None
    if result_path.is_file():
        try:
            result = read_json(result_path)
        except (OSError, ValueError, TypeError):
            result = None
    events = read_jsonl(trace_path)
    return TaskView(
        run_id=run_id,
        task_id=task_id,
        result=result,
        events=tuple(events),
        trace_path=str(trace_path) if trace_path.is_file() else None,
    )


def _load_demo_task(run: RunRef, task_id: str) -> TaskView:
    trace_path = run.root / f"{task_id}_trace.jsonl"
    events = read_jsonl(trace_path)
    return TaskView(
        run_id=run.run_id,
        task_id=task_id,
        result=None,
        events=tuple(events),
        trace_path=str(trace_path) if trace_path.is_file() else None,
    )


def _nodes_from_events(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    nodes: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "node_state_changed":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        node = data.get("node")
        if isinstance(node, Mapping) and node.get("node_id"):
            nodes[str(node["node_id"])] = dict(node)
    return tuple(nodes.values())


def _artifacts_from_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    artifacts: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "artifact_added":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        artifact = data.get("artifact")
        if isinstance(artifact, Mapping) and artifact.get("artifact_id"):
            key = f"{artifact['artifact_id']}@v{artifact.get('version', 1)}"
            artifacts[key] = dict(artifact)
    return tuple(artifacts.values())


def build_node_timeline(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return per-node status timeline ordered by state_version.

    Phase 5 机制 Trace 与 evaluation Trace 的事件格式一致，
    都通过 node_state_changed 携带节点快照。
    """

    timeline: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") not in {
            "node_state_changed",
            "node_finished",
        }:
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        node = data.get("node")
        if not isinstance(node, Mapping) or not node.get("node_id"):
            continue
        node_id = str(node["node_id"])
        version = int(data.get("state_version", 0))
        timeline.setdefault(node_id, []).append(
            {
                "state_version": version,
                "status": str(node.get("status", "UNKNOWN")),
                "started_at": node.get("started_at"),
                "finished_at": node.get("finished_at"),
                "failure_reason": node.get("failure_reason"),
            }
        )
    for entries in timeline.values():
        entries.sort(key=lambda item: item["state_version"])
    return timeline


def build_decision_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(event)
        for event in events
        if event.get("event_type")
        in {"supervisor_decision_applied", "supervisor_decision_rejected"}
    )


def build_model_call_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(event)
        for event in events
        if event.get("event_type")
        in {"model_call", "supervisor_decision_generated", "react_failed"}
    )


def rejection_summary(artifacts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(artifact)
        for artifact in artifacts
        if artifact.get("artifact_type") == "artifact_rejection"
    )
