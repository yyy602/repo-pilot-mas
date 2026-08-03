from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine
from repo_pilot_mas.schemas import TaskSpec
from repo_pilot_mas.state import CheckpointStore


def test_checkpoint_restores_closed_loop_engine(tmp_path: Path) -> None:
    engine = OrchestrationEngine(
        TaskSpec(
            task_id="checkpoint-test",
            repository_path=tmp_path,
            issue="restore closed loop state",
        )
    )
    engine.blackboard.set_stage("diagnosis")
    store = CheckpointStore(tmp_path / "checkpoints")

    store.save(engine, "closed-loop", thread_id="thread-1")
    restored = store.load(
        "closed-loop",
        expected_task_id="checkpoint-test",
        expected_thread_id="thread-1",
    )

    assert restored.engine.__class__.__module__.endswith("closed_loop_engine")
    assert restored.engine.task.task_id == engine.task.task_id
    assert restored.engine.blackboard.workflow_stage == "diagnosis"
