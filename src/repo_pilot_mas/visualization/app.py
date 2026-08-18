"""Streamlit dashboard for RepoPilot-MAS (Phase 7).

启动：
    streamlit run src/repo_pilot_mas/visualization/app.py
直接 import 解析器读取 reports/ 产物；阶段二提交与进度走 FastAPI 服务。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from repo_pilot_mas.visualization.parsers import (
    build_decision_events,
    build_model_call_events,
    build_node_timeline,
    discover_runs,
    load_run,
    load_task,
)
from repo_pilot_mas.visualization.runner import available_web_tasks

_NODE_TYPE_LEVELS = {
    "INVESTIGATION_TASK": 0,
    "DIAGNOSIS_TASK": 1,
    "CHALLENGE_TASK": 2,
    "REBUTTAL_TASK": 2,
    "REVIEW_TASK": 3,
    "PATCH_TASK": 4,
    "VALIDATION_TASK": 5,
    "REPLAN_TASK": 6,
    "FINALIZATION_TASK": 6,
}
_STATUS_COLORS = {
    "PENDING": "#9e9e9e",
    "READY": "#42a5f5",
    "RUNNING": "#ff9800",
    "SUCCEEDED": "#2e7d32",
    "FAILED": "#d32f2f",
    "TIMED_OUT": "#7b1fa2",
    "BLOCKED": "#546e7a",
    "PAUSED": "#f9a825",
    "CANCELLED": "#616161",
}
_STATUS_LABELS = {
    "PENDING": "等待中",
    "READY": "就绪",
    "RUNNING": "运行中",
    "SUCCEEDED": "成功",
    "FAILED": "失败",
    "TIMED_OUT": "超时",
    "BLOCKED": "阻塞",
    "PAUSED": "暂停",
    "CANCELLED": "已取消",
}


def _node_position(nodes: list[dict]) -> dict[str, tuple[int, int]]:
    positions: dict[str, tuple[int, int]] = {}
    for index, node in enumerate(nodes):
        level = _NODE_TYPE_LEVELS.get(str(node.get("node_type", "")), 7)
        positions[str(node["node_id"])] = (index, level)
    return positions


def _status_at(timeline: dict[str, list[dict]], node_id: str, version: int) -> str:
    entries = timeline.get(node_id, [])
    status = "PENDING"
    for entry in entries:
        if entry["state_version"] <= version:
            status = str(entry["status"])
        else:
            break
    return status


def render_graph(nodes: list[dict], timeline: dict[str, list[dict]], max_version: int) -> go.Figure:
    positions = _node_position(nodes)
    fig = go.Figure()
    # 依赖边
    for node in nodes:
        node_id = str(node["node_id"])
        for dep in node.get("dependencies", ()):
            if dep in positions and node_id in positions:
                x0, y0 = positions[dep]
                x1, y1 = positions[node_id]
                fig.add_trace(
                    go.Scatter(
                        x=[x0, x1],
                        y=[y0, y1],
                        mode="lines",
                        line={"color": "#999999", "width": 1},
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )
    xs: list[float] = []
    ys: list[float] = []
    labels: list[str] = []
    colors: list[str] = []
    sizes: list[int] = []
    for node in nodes:
        node_id = str(node["node_id"])
        x, y = positions[node_id]
        status = _status_at(timeline, node_id, max_version)
        xs.append(x)
        ys.append(y)
        labels.append(f"{node_id}<br>{node.get('node_type')}<br>{status}")
        colors.append(_STATUS_COLORS.get(status, "#eeeeee"))
        sizes.append(22 if node.get("node_type") == "CHALLENGE_TASK" or node.get("node_type") == "REBUTTAL_TASK" else 16)
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="markers+text",
            text=labels,
            textposition="top center",
            marker={"size": sizes, "color": colors, "line": {"width": 1, "color": "#333"}},
            hoverinfo="text",
            showlegend=False,
        )
    )
    # 状态图例：让各状态颜色一目了然
    for status, label in _STATUS_LABELS.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker={"size": 12, "color": _STATUS_COLORS.get(status, "#eeeeee")},
                name=label,
                showlegend=True,
            )
        )
    fig.update_layout(
        height=480,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        xaxis={"visible": False, "range": [-0.5, max(xs) + 0.5] if xs else [-1, 1]},
        yaxis={"visible": False, "autorange": "reversed"},
        title="动态任务图（按时间回放）",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
    )
    return fig


def render_dashboard() -> None:
    st.header("运行仪表盘")

    @st.fragment(run_every=3)
    def _dashboard_view() -> None:
        runs = discover_runs()
        if not runs:
            st.info("reports/ 下没有发现运行产物。")
            return
        rows = []
        for run in runs:
            rows.append(
                {
                    "run_id": run.run_id,
                    "split": run.split,
                    "demo": "机制演示" if run.demo else "",
                    "status": run.status or "",
                    "frozen": run.frozen,
                    "passed": run.passed,
                    "solved": run.solved,
                    "task_count": run.task_count,
                    "started_at": run.manifest.get("started_at", "")[:19],
                    "commit": (run.manifest.get("repository_commit") or "")[:8],
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        # 失败类别分布
        classes: dict[str, int] = {}
        for run in runs:
            if run.summary is None:
                continue
            for code, count in (
                run.summary.get("termination_failure_classes") or {}
            ).items():
                classes[str(code)] = classes.get(str(code), 0) + int(count)
        if classes:
            fig = go.Figure(
                go.Bar(x=list(classes), y=list(classes.values()), marker_color="#42a5f5")
            )
            fig.update_layout(title="全部运行失败类别分布", height=320)
            st.plotly_chart(fig, use_container_width=True)

    _dashboard_view()


def render_task_detail() -> None:
    st.header("任务详情")
    runs = discover_runs()
    if not runs:
        return
    labels = {}
    for run in runs:
        suffix = "·demo" if run.demo else ""
        if run.status:
            suffix += f"·{run.status}"
        labels[f"{run.run_id} ({run.split}{suffix})"] = run.run_id
    selected = st.selectbox("运行", list(labels))
    run = load_run(labels[selected])
    task_ids = run.task_ids
    if not task_ids:
        st.info("该运行暂无可见任务。")
        st.caption("运行中的任务需要先产生 Trace 才能看到；本页每 3 秒自动刷新。")
        return
    task_id = st.selectbox("任务", list(task_ids))
    initial = load_task(run.run_id, task_id)
    versions = sorted(
        {
            entry["state_version"]
            for entries in build_node_timeline(initial.events).values()
            for entry in entries
        }
    )
    max_version = (
        st.slider(
            "回放到 state_version",
            min_value=0,
            max_value=max(versions),
            value=max(versions),
        )
        if versions
        else 0
    )
    _task_view(run.run_id, task_id, max_version)


@st.fragment(run_every=3)
def _task_view(run_id: str, task_id: str, max_version: int) -> None:
    """运行中任务每 3 秒自动重读 Trace 刷新；完成后自动转为结果视图。"""

    task = load_task(run_id, task_id)
    st.subheader(f"{task.task_id} — {task.status}")
    if task.status == "demo":
        st.caption("机制演示 Trace（Phase 5），无真实结果文件。")
    elif task.result is None:
        st.info("任务仍在运行（暂无 result.json）：本页每 3 秒自动刷新。")
    if task.termination:
        st.json(
            {
                "code": task.termination.get("code"),
                "stage": task.termination.get("stage"),
                "failure_class": task.termination.get("failure_class"),
                "message": task.termination.get("message"),
            }
        )
    timeline = build_node_timeline(task.events)
    if not task.nodes:
        st.info("任务未创建任何节点即终止（例如 API 额度耗尽）。可在下方查看事件流与模型调用明细。")
    st.plotly_chart(
        render_graph(list(task.nodes), timeline, max_version),
        use_container_width=True,
    )
    col_left, col_right = st.columns(2)
    with col_left:
        with st.expander("节点时间线", expanded=False):
            rows = []
            for node_id, entries in timeline.items():
                for entry in entries:
                    rows.append(
                        {
                            "node_id": node_id,
                            "version": entry["state_version"],
                            "status": entry["status"],
                            "failure_reason": entry.get("failure_reason"),
                        }
                    )
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        with st.expander("模型调用明细", expanded=False):
            rows = []
            for event in build_model_call_events(task.events):
                data = event.get("data", {})
                rows.append(
                    {
                        "event": event.get("event_type"),
                        "model_id": data.get("model_id"),
                        "input_tokens": (data.get("usage") or {}).get("input_tokens")
                        if isinstance(data.get("usage"), dict)
                        else data.get("input_tokens"),
                        "output_tokens": (data.get("usage") or {}).get("output_tokens")
                        if isinstance(data.get("usage"), dict)
                        else data.get("output_tokens"),
                        "latency_ms": data.get("latency_ms"),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
    with col_right:
        with st.expander("Supervisor 决策", expanded=False):
            for event in build_decision_events(task.events):
                data = event.get("data", {})
                decision = data.get("decision") or {}
                action = data.get("code") or decision.get("action") or event.get("event_type")
                st.markdown(f"**{action}** — {str(decision.get('reason') or data.get('message') or '')[:160]}")
        with st.expander("Artifact 列表", expanded=False):
            for artifact in task.artifacts:
                content = artifact.get("content") or {}
                st.markdown(
                    f"- `{artifact.get('artifact_ref', artifact.get('artifact_id'))}` "
                    f"({artifact.get('artifact_type')}) — {str(content.get('claim') or content.get('verdict') or content.get('status') or '')[:80]}"
                )
                if artifact.get("artifact_type") == "patch_candidate":
                    st.code(str(content.get("diff", ""))[:2000], language="diff")


def render_compare() -> None:
    st.header("多运行对比")
    runs = discover_runs()
    if not runs:
        return
    labels = {
        run.run_id: run.run_id
        for run in runs
        if not run.demo
    }
    selected = st.multiselect("选择运行（对比）", list(labels))
    if not selected:
        return
    rows = []
    for run_id in selected:
        run = load_run(run_id)
        for task_id in run.task_ids:
            try:
                task = load_task(run_id, task_id)
            except KeyError:
                continue
            rows.append(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "status": task.status,
                    "failure_class": task.termination.get("failure_class"),
                    "code": task.termination.get("code"),
                    "stage": task.termination.get("stage"),
                    "duration_ms": (task.result or {}).get("usage", {}).get("duration_ms"),
                    "total_tokens": (task.result or {}).get("usage", {}).get("total_tokens"),
                }
            )
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


def _post_json(base_url: str, path: str, payload: dict) -> dict | None:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _get_json(base_url: str, path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def render_submit() -> None:
    st.header("运行任务")
    st.caption("通过 FastAPI 服务提交受信 QuixBugs 任务；产物写入 reports/web_runs/，与正式评测目录隔离。")
    last_run_id = st.session_state.get("last_run_id")
    if last_run_id:
        st.info(
            f"最近提交：**{last_run_id}**（到“任务详情”页选择该运行可实时查看；"
            "“审批队列”页处理人工审批。）"
        )
    base_url = st.text_input(
        "FastAPI 服务地址", value="http://127.0.0.1:8000"
    ).rstrip("/")
    tasks = available_web_tasks()
    if not tasks:
        st.warning("data/quixbugs/tasks/ 下没有受信任务清单。")
        return
    task_id = st.selectbox("任务", [task.task_id for task in tasks])
    run_label = st.text_input("run 标签（可选）", value="")
    require_approval = st.checkbox("需要人工审批（关键决策点挂起）", value=False)
    if st.button("提交运行", type="primary"):
        payload: dict = {"task_id": task_id, "require_human_approval": require_approval}
        if run_label.strip():
            payload["run_id"] = run_label.strip()
        response = _post_json(base_url, "/runs", payload)
        if response is None or "run_id" not in response:
            st.error(
                "提交失败：请确认 FastAPI 服务已启动"
                "（uvicorn repo_pilot_mas.visualization.service:app）。"
            )
            return
        run_id = response["run_id"]
        st.session_state["last_run_id"] = run_id
        st.success(f"已提交：{run_id}")
        placeholder = st.empty()
        record: dict | None = None
        for _ in range(600):  # 最长轮询 20 分钟
            record = _get_json(base_url, f"/runs/{run_id}/status")
            if record is None:
                placeholder.error("状态查询失败（服务不可达？）")
                return
            message = f"状态：**{record.get('status')}**"
            if record.get("error"):
                message += f" | 错误：{record['error']}"
            if record.get("trace_lines"):
                message += f" | Trace 行数：{record['trace_lines']}"
            placeholder.info(message)
            if record.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        if record is not None and record.get("status") == "done":
            st.success(f"运行完成：{run_id}。到“任务详情”页选择该运行查看结果。")
        elif record is not None and record.get("status") == "failed":
            st.error(f"运行失败：{record.get('error')}")


def render_approvals() -> None:
    st.header("审批队列（人工审批控制）")
    st.caption(
        "审批模式下任务在关键决策点挂起；批准后继续派发，拒绝后返回 Supervisor 重新决策。"
    )
    base_url = st.text_input(
        "FastAPI 服务地址", value="http://127.0.0.1:8000"
    ).rstrip("/")
    approvals = _get_json(base_url, "/approvals")
    if approvals is None:
        st.error("服务不可达（需启动 server.py）或运行模式未启用。")
        return
    pending = approvals.get("approvals") or []
    if not pending:
        st.info("当前没有挂起审批。")
        return
    for item in pending:
        decision = item.get("decision") or {}
        with st.container(border=True):
            st.markdown(
                f"**{item.get('run_id')}** · 任务 `{item.get('task_id')}` · "
                f"决策 `{decision.get('decision_id') or item.get('decision_id')}` · "
                f"action `{decision.get('action') or '?'}` · state_version "
                f"`{item.get('state_version')}`"
            )
            if decision.get("reason"):
                st.markdown(f"理由：{decision['reason']}")
            if item.get("ready_worker_ids"):
                st.caption(f"待派发 Worker：{', '.join(item['ready_worker_ids'])}")
            col_approve, col_reject = st.columns(2)
            if col_approve.button("批准", key=f"appr-{item['run_id']}", type="primary"):
                result = _post_json(
                    base_url,
                    f"/runs/{item['run_id']}/approve",
                    {"approved": True},
                )
                st.success("已批准" if result else "批准失败（可能已响应）")
            if col_reject.button("拒绝", key=f"rej-{item['run_id']}"):
                result = _post_json(
                    base_url,
                    f"/runs/{item['run_id']}/approve",
                    {"approved": False},
                )
                st.success("已拒绝" if result else "拒绝失败（可能已响应）")
    if st.button("刷新"):
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="RepoPilot-MAS 可视化", layout="wide")
    st.title("RepoPilot-MAS 可视化")
    page = st.sidebar.radio("页面", ["仪表盘", "任务详情", "对比", "运行任务", "审批队列"])
    if page == "仪表盘":
        render_dashboard()
    elif page == "任务详情":
        render_task_detail()
    elif page == "对比":
        render_compare()
    elif page == "运行任务":
        render_submit()
    else:
        render_approvals()


if __name__ == "__main__":
    main()
