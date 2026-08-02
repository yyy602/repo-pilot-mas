"""经 LangGraph 运行一次真实 Supervisor 决策并保存脱敏验收证据。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from repo_pilot_mas.agents import SupervisorAgent
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.models import create_supervisor_model_router
from repo_pilot_mas.orchestration import (
    EngineBudget,
    FakeWorkerExecutor,
    LangGraphRuntime,
    OrchestrationEngine,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.runtime import TraceWriter, redact_secrets
from repo_pilot_mas.state import CheckpointStore
from repo_pilot_mas.tasks import load_task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="TaskSpec JSON 文件")
    parser.add_argument("--supervisor-config", default="configs/supervisor.yaml")
    parser.add_argument("--runtime-config", default="configs/runtime.yaml")
    parser.add_argument("--report-root", default="reports/phase3/acceptance/real_supervisor")
    args = parser.parse_args()

    task = load_task(args.task)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = Path(args.report_root).expanduser().resolve(strict=False) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    trace = TraceWriter(run_root / "trace.jsonl")
    router, generation = create_supervisor_model_router(
        args.supervisor_config,
        raw_log_dir=run_root / "raw_model_responses",
        trace_writer=trace,
    )
    supervisor = SupervisorAgent(
        router,
        generation_config=generation,
        trace_writer=trace,
    )
    runtime_config = load_yaml(args.runtime_config)
    orchestration = runtime_config.get("orchestration", {})
    langgraph = runtime_config.get("langgraph", {})
    if not isinstance(orchestration, dict) or not isinstance(langgraph, dict):
        raise TypeError("orchestration and langgraph runtime configuration must be mappings")
    engine = OrchestrationEngine(
        task,
        budget=EngineBudget(**orchestration),
        trace_writer=trace,
    )
    thread_id = f"{task.task_id}-{run_id}"
    checkpoint_db = run_root / "langgraph.sqlite3"
    fake_worker = FakeWorkerExecutor()
    with LangGraphRuntime(
        supervisor,
        fake_worker,
        checkpoint_db,
        trace_writer=trace,
        recursion_limit=int(langgraph.get("recursion_limit", 128)),
    ) as runtime:
        state = runtime.run(
            engine,
            thread_id=thread_id,
            require_human_approval=True,
        )
        snapshot = runtime.graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        history_length = len(
            runtime.state_history(thread_id=thread_id, task_id=task.task_id)
        )
        waiting_for_human = any(task.interrupts for task in snapshot.tasks)

    restored_engine = restore_engine_from_runtime_state(state, trace_writer=trace)
    checkpoint_path = CheckpointStore(run_root / "audit_checkpoints").save(
        restored_engine,
        "after-langgraph-supervisor-decision",
        router_state=router.to_state_dict(),
        workspace_refs={},
        thread_id=thread_id,
    )
    decision_result = state.get("decision_result")
    report = {
        "phase": 3,
        "run_id": run_id,
        "task_id": task.task_id,
        "decision_result": decision_result,
        "engine_status": restored_engine.status.value,
        "workflow_stage": restored_engine.blackboard.workflow_stage,
        "nodes": [node.to_dict() for node in restored_engine.graph.nodes],
        "supervisor_call": restored_engine.last_supervisor_call,
        "router_state": router.to_state_dict(),
        "langgraph": {
            "thread_id": thread_id,
            "state_version": restored_engine.state_version,
            "checkpoint_db": str(checkpoint_db),
            "history_length": history_length,
            "waiting_for_human": waiting_for_human,
            "next_nodes": list(snapshot.next),
            "worker_calls": list(fake_worker.calls),
        },
        "trace_path": str(trace.path),
        "audit_checkpoint_path": str(checkpoint_path),
    }
    report_path = run_root / "report.json"
    safe_report = redact_secrets(report)
    report_path.write_text(
        json.dumps(safe_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(safe_report, ensure_ascii=False, indent=2))
    decision_ok = isinstance(decision_result, dict) and decision_result.get("ok") is True
    return 0 if decision_ok and waiting_for_human and not fake_worker.calls else 1


if __name__ == "__main__":
    raise SystemExit(main())
