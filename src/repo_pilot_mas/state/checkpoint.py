"""Atomic, checksummed Phase 3 checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from repo_pilot_mas.runtime.trace import TraceWriter
from repo_pilot_mas.schemas.tool_result import utc_now_iso

if TYPE_CHECKING:
    from repo_pilot_mas.orchestration.engine import OrchestrationEngine

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PAYLOAD_FILENAMES = frozenset(
    {
        "task_state.json",
        "task_graph.json",
        "blackboard.json",
        "trace.jsonl",
        "router_state.json",
        "workspace_refs.json",
    }
)


@dataclass(frozen=True, slots=True)
class CheckpointBundle:
    engine: OrchestrationEngine
    router_state: Mapping[str, Any]
    workspace_refs: Mapping[str, Any]
    trace_path: Path
    checkpoint_path: Path
    thread_id: str | None = None


class CheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        engine: OrchestrationEngine,
        name: str,
        *,
        router_state: Mapping[str, Any] | None = None,
        workspace_refs: Mapping[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> Path:
        _validate_name(name)
        if thread_id is not None:
            _validate_name(thread_id)
        target = self.root / name
        if target.exists():
            raise FileExistsError(f"checkpoint already exists: {target}")
        temporary = self.root / f".{name}.tmp-{uuid4().hex}"
        temporary.mkdir()
        try:
            payloads = {
                "task_state.json": engine.to_task_state_dict(),
                "task_graph.json": engine.graph.to_dict(),
                "blackboard.json": engine.blackboard.to_dict(),
                "router_state.json": dict(router_state or {}),
                "workspace_refs.json": dict(workspace_refs or {}),
            }
            checksums: dict[str, str] = {}
            for filename, payload in payloads.items():
                encoded = _encoded_json(payload)
                (temporary / filename).write_bytes(encoded)
                checksums[filename] = hashlib.sha256(encoded).hexdigest()
            trace_bytes = b""
            if engine.trace_writer is not None:
                trace_path = engine.trace_writer.path
                if trace_path.is_symlink():
                    raise ValueError("trace path must not be a symbolic link")
                if trace_path.is_file():
                    trace_bytes = trace_path.read_bytes()
            (temporary / "trace.jsonl").write_bytes(trace_bytes)
            checksums["trace.jsonl"] = hashlib.sha256(trace_bytes).hexdigest()
            manifest = {
                "checkpoint_name": name,
                "task_id": engine.task.task_id,
                "state_version": engine.state_version,
                "thread_id": thread_id,
                "created_at": utc_now_iso(),
                "files": checksums,
            }
            (temporary / "manifest.json").write_bytes(_encoded_json(manifest))
            temporary.rename(target)
            return target
        except Exception:
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise

    def load(
        self,
        name: str,
        *,
        trace_writer: TraceWriter | None = None,
        expected_task_id: str | None = None,
        expected_thread_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> CheckpointBundle:
        from repo_pilot_mas.orchestration.engine import OrchestrationEngine

        _validate_name(name)
        target = self.root / name
        if target.is_symlink() or not target.is_dir():
            raise ValueError("checkpoint directory is missing or unsafe")
        manifest = _read_json(target / "manifest.json")
        if manifest.get("checkpoint_name") != name:
            raise ValueError("checkpoint manifest name is inconsistent")
        files = manifest.get("files")
        if not isinstance(files, Mapping):
            raise TypeError("checkpoint manifest has no file checksums")
        if set(files) != _PAYLOAD_FILENAMES:
            raise ValueError("checkpoint manifest file set is invalid")
        payloads: dict[str, Mapping[str, Any]] = {}
        for filename, expected_digest in files.items():
            path = target / str(filename)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"checkpoint file is missing or unsafe: {filename}")
            encoded = path.read_bytes()
            if hashlib.sha256(encoded).hexdigest() != expected_digest:
                raise ValueError(f"checkpoint checksum mismatch: {filename}")
            if filename == "trace.jsonl":
                continue
            value = json.loads(encoded)
            if not isinstance(value, Mapping):
                raise TypeError(f"checkpoint file must contain an object: {filename}")
            payloads[str(filename)] = value

        engine = OrchestrationEngine.restore(
            payloads["task_state.json"],
            payloads["task_graph.json"],
            payloads["blackboard.json"],
            trace_writer=trace_writer,
        )
        if manifest.get("task_id") != engine.task.task_id:
            raise ValueError("checkpoint manifest task_id is inconsistent")
        if int(manifest.get("state_version", -1)) != engine.state_version:
            raise ValueError("checkpoint manifest state_version is inconsistent")
        if expected_task_id is not None and engine.task.task_id != expected_task_id:
            raise ValueError("checkpoint task_id does not match the runtime binding")
        if expected_thread_id is not None and manifest.get("thread_id") != expected_thread_id:
            raise ValueError("checkpoint thread_id does not match the runtime binding")
        if expected_state_version is not None and engine.state_version != expected_state_version:
            raise ValueError("checkpoint state_version does not match the runtime binding")
        return CheckpointBundle(
            engine=engine,
            router_state=payloads["router_state.json"],
            workspace_refs=payloads["workspace_refs.json"],
            trace_path=target / "trace.jsonl",
            checkpoint_path=target,
            thread_id=str(manifest["thread_id"]) if manifest.get("thread_id") else None,
        )


def _validate_name(name: str) -> None:
    if not _NAME_PATTERN.fullmatch(name) or ".." in name:
        raise ValueError(f"invalid checkpoint name: {name!r}")


def _encoded_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _read_json(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"checkpoint file is missing or unsafe: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"checkpoint file must contain an object: {path.name}")
    return value
