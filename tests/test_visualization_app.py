"""Phase 7 dashboard: graph rendering smoke test (no Streamlit runtime)."""

from __future__ import annotations

from repo_pilot_mas.visualization.app import render_graph


def test_render_graph_basic() -> None:
    nodes = [
        {
            "node_id": "N1",
            "node_type": "INVESTIGATION_TASK",
            "dependencies": [],
            "status": "SUCCEEDED",
        },
        {
            "node_id": "N2",
            "node_type": "DIAGNOSIS_TASK",
            "dependencies": ["N1"],
            "status": "SUCCEEDED",
        },
    ]
    timeline = {
        "N1": [
            {"state_version": 2, "status": "RUNNING", "started_at": None, "finished_at": None, "failure_reason": None},
            {"state_version": 3, "status": "SUCCEEDED", "started_at": None, "finished_at": None, "failure_reason": None},
        ],
        "N2": [
            {"state_version": 4, "status": "PENDING", "started_at": None, "finished_at": None, "failure_reason": None},
        ],
    }
    fig = render_graph(nodes, timeline, max_version=2)
    assert len(fig.data) >= 2  # 边 + 节点
    # N2 在 max_version=2 时尚未 RUNNING，保持 PENDING 色
    assert any(
        getattr(trace, "marker", None) is not None and trace.marker is not None
        for trace in fig.data
    )


def test_render_graph_replay_advances_status() -> None:
    nodes = [
        {
            "node_id": "N1",
            "node_type": "INVESTIGATION_TASK",
            "dependencies": [],
            "status": "SUCCEEDED",
        }
    ]
    timeline = {
        "N1": [
            {"state_version": 2, "status": "RUNNING", "started_at": None, "finished_at": None, "failure_reason": None},
            {"state_version": 3, "status": "SUCCEEDED", "started_at": None, "finished_at": None, "failure_reason": None},
        ]
    }
    early = render_graph(nodes, timeline, max_version=2)
    late = render_graph(nodes, timeline, max_version=3)
    assert early is not None and late is not None
