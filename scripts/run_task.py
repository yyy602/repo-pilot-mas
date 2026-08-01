"""运行一个 RepoPilot-MAS Phase 2 Single-Agent 任务。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import SingleAgent
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.models import GenerationConfig, create_model_adapter
from repo_pilot_mas.orchestration import ReactBudget
from repo_pilot_mas.tasks import load_task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="TaskSpec JSON 文件")
    parser.add_argument("--model-config", default="configs/model.yaml")
    parser.add_argument("--runtime-config", default="configs/runtime.yaml")
    parser.add_argument("--report-root", default="reports/phase2")
    parser.add_argument("--workspace-root", default=".repo_pilot_workspaces")
    args = parser.parse_args()

    task = load_task(args.task)
    model_config = load_yaml(args.model_config)
    runtime_config = load_yaml(args.runtime_config)
    report_root = Path(args.report_root).expanduser().resolve(strict=False)
    model = create_model_adapter(
        _mapping(model_config, "model"),
        raw_log_dir=report_root / "raw_model_responses",
    )
    generation = _mapping(model_config, "generation")
    budget_config = _mapping(runtime_config, "single_agent")
    agent = SingleAgent(
        model,
        workspace_root=args.workspace_root,
        report_root=report_root / "runs",
        generation_config=GenerationConfig(
            temperature=float(generation.get("temperature", 0.0)),
            max_output_tokens=int(generation.get("max_output_tokens", 512)),
            stop=tuple(str(item) for item in generation.get("stop", ())),
            timeout_seconds=float(generation.get("timeout_seconds", 120)),
            max_retries=int(generation.get("max_retries", 1)),
        ),
        budget=ReactBudget(
            max_steps=int(budget_config.get("max_steps", 10)),
            max_model_calls=int(budget_config.get("max_model_calls", 12)),
            max_tool_calls=int(budget_config.get("max_tool_calls", 12)),
            max_input_tokens=int(budget_config.get("max_input_tokens", 40_000)),
            max_output_tokens=int(budget_config.get("max_output_tokens", 8_000)),
            max_runtime_seconds=float(budget_config.get("max_runtime_seconds", 300)),
        ),
    )
    report = agent.run(task)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.status == "succeeded" else 1


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    selected = value.get(key, {})
    if not isinstance(selected, dict):
        raise TypeError(f"configuration section must be a mapping: {key}")
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
