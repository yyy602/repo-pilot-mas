# Phase 7 可视化驾驶舱使用说明

> 阶段一（只读驾驶舱）、阶段二（常驻运行模式）、阶段三（人工审批控制面）均已完成。

## 1. 安装依赖

```bash
conda activate multi_agent
cd /home/user50305/yjh/repo-pilot-mas
python -m pip install -e ".[dev,visualization]"
```

依赖组 `visualization`：fastapi、uvicorn、streamlit、plotly、pandas。

## 2. 启动服务（阶段二：常驻运行模式）

两个终端：

```bash
# 终端 1：FastAPI 服务（常驻 Worker 模型池；首次提交任务时加载模型）
python -m repo_pilot_mas.visualization.server

# 终端 2：Streamlit 驾驶舱
streamlit run src/repo_pilot_mas/visualization/app.py \
  --server.headless true --server.port 8501
```

浏览器打开 http://localhost:8501 → 页面选"运行任务" → 选择任务提交，页面实时轮询进度；完成后到"任务详情"选择对应 web 运行查看结果。

### 接口一览

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /runs` | 全部运行（含机制演示与 web 运行）列表 |
| `GET /runs/{run_id}` | 单个运行摘要 |
| `GET /runs/{run_id}/tasks/{task_id}` | 任务详情（节点、Artifact、节点时间线、决策、模型调用、rejection） |
| `GET /runs/{run_id}/compare` | 单运行逐任务对比 |
| `POST /runs` | 提交受信任务（JSON：`{"task_id": "quixbugs_gcd", "run_id": "可选", "require_human_approval": true/false}`） |
| `GET /runs/{run_id}/status` | web 运行进度（queued/preparing/running/done/failed） |
| `GET /approvals` | 全部挂起审批（阶段三） |
| `GET /runs/{run_id}/pending` | 单个运行挂起审批详情 |
| `POST /runs/{run_id}/approve` | 批准/拒绝（JSON：`{"approved": true/false}`） |

示例：

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "quixbugs_gcd"}'
```

## 3. 页面

- **仪表盘**：运行列表（含 `web` split 与机制演示）、失败类别分布；
- **任务详情**：终止信息、动态任务图（滑块回放）、节点时间线、决策、Artifact/Patch diff、模型调用明细；
- **对比**：多运行逐任务对比；
- **运行任务**：提交受信 QuixBugs 任务并实时轮询进度（可勾选"需要人工审批"）；
- **审批队列**（阶段三）：轮询挂起审批，展示决策 action/理由/待派发 Worker，一键批准或拒绝；批准后任务继续，拒绝后不派发并返回 Supervisor 重新决策（对应 `human_rejected` 路径）。

## 4. 数据来源与隔离

- 真实评测运行：`reports/closed_loop/evaluation/<run_id>/`；
- **web 运行：`reports/web_runs/<run_id>/`，与正式评测目录严格隔离，不参与冻结身份**；
- 机制演示（Phase 5）：`reports/phase5/acceptance/<id>/<name>_trace.jsonl`，UI 以 `phase5:` 前缀标注；
- 任务白名单：`data/quixbugs/tasks/*.json`（不开放任意仓库任务）。

## 5. 安全与边界

- 服务默认只读；运行接口仅 `server.py`（显式创建 runner）启用，bind 127.0.0.1；
- 不修改 Engine/Supervisor/Worker；web 运行不影响评测与冻结流程；
- 阶段三将在此基础上增加关键决策的人工审批（`require_human_approval` / `resume`）。
